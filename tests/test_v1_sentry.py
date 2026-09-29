"""examples/v1/sentry-to-github-pr.outcome.yaml, run end to end.

The agent CLI is replaced by a scripted agent that calls capabilities through
the real broker socket, and Slack and GitHub are served by a mock transport,
so the grants, the recorded calls, the await step and the skips all run
through the real runtime.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

from outcomeci import integrations, local, run_container, v1_runtime
from outcomeci.capability import invoke_integration
from outcomeci.config import compile_workflow
from outcomeci.process import ExecutionError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "v1"
WORKFLOW = "sentry-to-github-pr.outcome.yaml"
SENTRY_ALERT = {
    "action": "triggered",
    "data": {"issue": {"title": "KeyError: 'plan'", "culprit": "app.billing", "level": "error"}},
}


def _payload() -> dict:
    return {
        "schema_version": "outcomeci.trigger.webhook.received/v1",
        "type": "webhook.received",
        "event_id": "evt-1",
        "received_at": "2026-09-28T12:00:00Z",
        "method": "POST",
        "query": "",
        "headers": {"content-type": "application/json"},
        "body_base64": base64.b64encode(json.dumps(SENTRY_ALERT).encode()).decode(),
    }


class Services:
    """Slack and GitHub, recording every request the runtime sends."""

    def __init__(self, *, reacted: bool = True):
        self.requests: list[httpx.Request] = []
        self.reacted = reacted

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "slack.com" and path == "/api/chat.postMessage":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channel": "C0SENTRY",
                    "ts": f"17.{len(self.requests)}",
                    "echo": body,
                },
            )
        if request.url.host == "slack.com" and path == "/api/reactions.get":
            reactions = [{"name": "+1", "count": 1}] if self.reacted else []
            return httpx.Response(200, json={"ok": True, "message": {"reactions": reactions}})
        if request.url.host == "api.github.com" and path.endswith("/pulls"):
            return httpx.Response(
                201, json={"html_url": "https://github.com/outcomeci/cli/pull/7", "number": 7}
            )
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    def sent(self, host: str, path: str) -> list[httpx.Request]:
        return [item for item in self.requests if item.url.host == host and item.url.path == path]


def _call(env: dict, capability: str, inputs: dict) -> dict:
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        return invoke_integration(capability, inputs)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _context(prompt: str) -> dict:
    return json.loads(prompt.rsplit("\n", 1)[-1])


class Agent:
    """Plays each step the way the instructions ask, through the broker."""

    def __init__(self, decision: str = "fix"):
        self.decision = decision
        self.prompts: dict[str, dict] = {}
        self.denials: list[str] = []

    def __call__(self, runner, model, prompt, root, timeout, **kwargs):
        env = kwargs["extra_env"]
        context = _context(prompt)
        step = context["step"]
        self.prompts[step] = context
        if step == "triage":
            try:
                _call(env, "slack.post", {"channel": "general", "text": "wrong channel"})
            except ExecutionError as exc:
                self.denials.append(str(exc))
            _call(env, "slack.post", {"text": "*Sentry alert -- KeyError: 'plan'*"})
            outputs = {
                "decision": self.decision,
                "issue": {
                    "title": "KeyError: 'plan'",
                    "message": "KeyError: 'plan'",
                    "culprit": "app.billing",
                    "level": "error",
                    "project": "cli",
                    "url": "https://sentry.io/issues/1",
                },
                "reason": "billing code bug",
                **(
                    {"repo": {"owner": "outcomeci", "name": "cli"}}
                    if self.decision == "fix"
                    else {}
                ),
            }
        elif step == "fix":
            try:
                _call(
                    env,
                    "github.write",
                    {"method": "POST", "path": "/repos/other/repo/pulls", "body": {}},
                )
            except ExecutionError as exc:
                self.denials.append(str(exc))
            pr = _call(
                env,
                "github.write",
                {"method": "POST", "path": "/repos/outcomeci/cli/pulls", "body": {"title": "fix"}},
            )
            outputs = {
                "pr": {
                    "url": pr["output"]["result"]["html_url"],
                    "number": pr["output"]["result"]["number"],
                    "branch": "sentry-fix/1",
                }
            }
        elif step == "announce":
            try:
                _call(env, "slack.post", {"text": "PR opened", "thread_ts": "99.9"})
            except ExecutionError as exc:
                self.denials.append(str(exc))
            _call(env, "slack.post", {"text": "PR opened"})
            outputs = None
        else:
            raise AssertionError(f"unexpected step {step}")
        if outputs is not None:
            path = Path(context["returns"]["path"])
            path.write_text(json.dumps(outputs), encoding="utf-8")
        return f"{step} done"


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(EXAMPLES, tmp_path / "wf")
    root = tmp_path / "wf"
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(v1_runtime.time, "sleep", lambda seconds: None)
    return root


def _serve(monkeypatch, services: Services) -> None:
    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(services)
        return real(*args, **kwargs)

    monkeypatch.setattr(integrations.httpx, "Client", client)


def _run(root: Path, agent: Agent, monkeypatch, reviews: list | None = None) -> dict:
    monkeypatch.setattr(local, "invoke", agent)
    config = root / WORKFLOW

    def review(proposal):
        if reviews is not None:
            reviews.append(proposal)
        return {"decision": "allow", "proposal_sha256": proposal["proposal_sha256"], "reason": "ok"}

    options = local.ExecutionOptions(
        credential_resolver=lambda reference: "xoxb-or-ghp-token",
        policy_reviewer=review,
    )
    return run_container.execute(
        root, config, compile_workflow(config), "webhook", _payload(), options, auto_continue=True
    )


def test_approved_alert_runs_every_step_inside_its_grants(workflow, monkeypatch):
    services = Services()
    _serve(monkeypatch, services)
    agent = Agent()
    reviews: list = []

    result = _run(workflow, agent, monkeypatch, reviews)

    assert result["status"] == "completed"
    assert result["completed_phases"] == ["triage", "approve", "fix", "announce"]
    assert result.get("skipped_phases", []) == []
    posts = [
        json.loads(item.content) for item in services.sent("slack.com", "/api/chat.postMessage")
    ]
    assert [post["channel"] for post in posts] == ["sentry", "C0SENTRY", "sentry"]
    # Before waiting, the runtime says in the alert's thread what approving allows,
    # resolved from triage's result rather than from the text triage posted.
    notice = posts[1]
    assert notice["thread_ts"] == "17.1"
    assert notice["text"].startswith("Reacting :+1: approves exactly this:")
    assert "- fix: github.write on repo outcomeci/cli" in notice["text"]
    # The PR link is a reply in the alert's thread, filled in by the grant.
    assert posts[2]["thread_ts"] == "17.1"
    assert any("thread_ts must be 17.1" in denial for denial in agent.denials)
    # The await step polled the reaction on the message triage recorded.
    polled = services.sent("slack.com", "/api/reactions.get")
    assert polled and polled[0].url.params["channel"] == "C0SENTRY"
    pulls = services.sent("api.github.com", "/repos/outcomeci/cli/pulls")
    assert len(pulls) == 1
    assert not services.sent("api.github.com", "/repos/other/repo/pulls")
    assert any("channel must be sentry" in denial for denial in agent.denials)
    assert any("path must be under /repos/outcomeci/cli" in denial for denial in agent.denials)
    # Only the fix step has a policy, and only its write was reviewed.
    assert [proposal["request"]["path"] for proposal in reviews] == ["/repos/outcomeci/cli/pulls"]
    assert "Step policy for fix" in reviews[0]["policy"]["content"]
    # Steps receive references resolved from earlier steps.
    fix_inputs = {item["name"]: item["value"] for item in agent.prompts["fix"]["inputs"]}
    assert fix_inputs["triage"]["repo"] == {"owner": "outcomeci", "name": "cli"}
    announce_inputs = {item["name"]: item["value"] for item in agent.prompts["announce"]["inputs"]}
    assert announce_inputs["fix"]["pr"]["number"] == 7
    assert agent.prompts["fix"]["grants"][0]["args"] == {
        "repo": {"owner": "outcomeci", "name": "cli"}
    }


def test_recorded_calls_carry_the_step_that_made_them(workflow, monkeypatch):
    services = Services()
    _serve(monkeypatch, services)
    result = _run(workflow, Agent(), monkeypatch)

    message = v1_runtime.value(workflow, result, "triage.calls.slack.post")
    assert message["channel"] == "C0SENTRY"
    polled = services.sent("slack.com", "/api/reactions.get")[0]
    assert polled.url.params["timestamp"] == message["ts"]
    journal = json.loads(
        (workflow / ".outcomeci" / ".broker" / result["run_id"] / "journal.json").read_text()
    )
    phases = sorted({call["phase"] for call in journal["calls"].values()})
    assert phases == ["announce", "fix", "triage"]


def test_no_op_triage_skips_the_approval_fix_and_announcement(workflow, monkeypatch):
    services = Services()
    _serve(monkeypatch, services)

    result = _run(workflow, Agent(decision="no_op"), monkeypatch)

    assert result["status"] == "completed"
    assert result["skipped_phases"] == ["approve", "fix", "announce"]
    assert result["skip_reasons"]["announce"] == "reads skipped step fix"
    assert not services.sent("slack.com", "/api/reactions.get")
    assert len(services.sent("slack.com", "/api/chat.postMessage")) == 1


def test_an_expired_approval_skips_everything_after_it(workflow, monkeypatch):
    services = Services(reacted=False)
    _serve(monkeypatch, services)
    clock = iter(range(0, 10**6, 1000))
    monkeypatch.setattr(v1_runtime.time, "monotonic", lambda: next(clock))

    result = _run(workflow, Agent(), monkeypatch)

    assert result["status"] == "completed"
    assert result["skipped_phases"] == ["approve", "fix", "announce"]
    assert not services.sent("api.github.com", "/repos/outcomeci/cli/pulls")
    interaction = json.loads(
        (
            workflow
            / ".outcomeci/outcomes"
            / result["run_id"]
            / "interactions/approve/approve.json"
        ).read_text()
    )
    assert interaction["status"] == "expired"
