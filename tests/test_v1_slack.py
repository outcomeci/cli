"""examples/v1/slack-to-github-pr.outcome.yaml, run end to end.

A scripted agent plays every step and discussion turn through the real
broker; a mock Slack thread releases each human message only after the bot
answered the one before, so the discussion fold, the for_each fan-out and its
per-item grants all run through the real runtime.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from pathlib import Path

import httpx
import pytest

from outcomeci import debug, integrations, local, v1_runtime
from outcomeci.capability import invoke_integration
from outcomeci.config import compile_workflow
from outcomeci.process import ExecutionError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "v1"
WORKFLOW = "slack-to-github-pr.outcome.yaml"
REQUEST = {
    "channel": "C0BUILD",
    "user": "U1",
    "ts": "100.0",
    "text": "Add a --json flag to `outcomeci/cli` and accept it in `outcomeci/api`",
}
PLAN = {
    "summary": "Add --json output",
    "repos": [
        {"repo": {"owner": "outcomeci", "name": "cli"}, "steps": ["add the flag"]},
        {"repo": {"owner": "outcomeci", "name": "api"}, "steps": ["accept json"]},
    ],
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
        "body_base64": base64.b64encode(json.dumps(REQUEST).encode()).decode(),
    }


class Slack:
    """One channel whose thread releases scripted human replies one at a time."""

    def __init__(self, humans: list[str | dict]):
        # A human reply is its text, or {"text", "files"} for one with attachments.
        self.humans = list(humans)
        self.root = REQUEST["ts"]
        self.thread: list[dict] = [{"ts": REQUEST["ts"], "text": REQUEST["text"], "user": "U1"}]
        self.posts: list[dict] = []
        self.pulls: list[str] = []
        self.clock = 200

    def _ts(self) -> str:
        self.clock += 1
        return f"{self.clock}.0"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.github.com":
            if path.endswith("/pulls") and request.method == "POST":
                self.pulls.append(path)
                number = len(self.pulls)
                return httpx.Response(
                    201, json={"html_url": f"https://github.com{path}/{number}", "number": number}
                )
            return httpx.Response(200, json={"ok": True})
        if path == "/api/chat.postMessage":
            body = json.loads(request.content)
            ts = self._ts()
            self.posts.append({**body, "ts": ts})
            response = {"ok": True, "channel": body["channel"], "ts": ts}
            if body.get("thread_ts") == self.root:
                self.thread.append({"ts": ts, "text": body["text"], "bot_id": "B1"})
                response["message"] = {"thread_ts": self.root}
            return httpx.Response(200, json=response)
        if path == "/api/conversations.replies":
            # Slack returns the whole thread only for its root message.
            assert request.url.params["ts"] == self.root
            if self.humans and self.thread[-1].get("bot_id"):
                human = self.humans.pop(0)
                human = human if isinstance(human, dict) else {"text": human}
                self.thread.append({"ts": self._ts(), "user": "U1", **human})
            return httpx.Response(200, json={"ok": True, "messages": list(self.thread)})
        if path == "/api/files.info":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "file": {
                        "name": "error.png",
                        "mimetype": "image/png",
                        "size": 4,
                        "channels": [REQUEST["channel"]],
                        "url_private_download": "https://files.slack.com/files-pri/T-F/error.png",
                    },
                },
            )
        if request.url.host == "files.slack.com":
            return httpx.Response(200, content=b"\x89PNG")
        return httpx.Response(404, json={"ok": False, "error": "not_found"})


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


class Agent:
    def __init__(self):
        self.steps: list[dict] = []
        self.turns: list[str] = []
        self.files: list[dict] = []
        self.opened: list[dict] = []
        self.open_request_files = False
        self.denials: list[str] = []

    def __call__(self, runner, model, prompt, root, timeout, **kwargs):
        if "You are taking one turn in a discussion" in prompt:
            return self._turn(prompt)
        env = kwargs["extra_env"]
        context = json.loads(prompt.rsplit("\n", 1)[-1])
        self.steps.append({"runner": runner, "model": model, **context})
        inputs = {item["name"]: item["value"] for item in context["inputs"]}
        step, outputs = context["step"], None
        if step == "draft":
            if self.open_request_files:
                self.opened.append(_call(env, "slack.file", {"file": "F0SHOT"})["output"])
            _call(env, "slack.post", {"text": "Plan: add --json", "thread_ts": REQUEST["ts"]})
            outputs = {"plan": PLAN}
        elif step == "implement":
            owner, name = inputs["target"]["repo"]["owner"], inputs["target"]["repo"]["name"]
            other = "api" if name == "cli" else "cli"
            try:
                _call(
                    env,
                    "github.write",
                    {"method": "POST", "path": f"/repos/outcomeci/{other}/pulls"},
                )
            except ExecutionError as exc:
                self.denials.append(str(exc))
            pr = _call(
                env, "github.write", {"method": "POST", "path": f"/repos/{owner}/{name}/pulls"}
            )
            result = pr["output"]["result"]
            outputs = {"pr": {"url": result["html_url"], "number": result["number"], "branch": "b"}}
        elif step == "announce":
            links = ", ".join(item["url"] for item in inputs["pr"] if item)
            _call(env, "slack.post", {"text": f"PRs: {links}", "thread_ts": REQUEST["ts"]})
        else:
            raise AssertionError(f"unexpected step {step}")
        if outputs is not None:
            Path(context["returns"]["path"]).write_text(json.dumps(outputs), encoding="utf-8")
        return f"{step} done"

    def _turn(self, prompt: str) -> str:
        path = Path(re.search(r"to (/\S+\.json)\.", prompt).group(1))
        plan = json.loads(re.search(r"Current plan \(version \d+\): (.+)\n", prompt).group(1))
        turns = json.loads(re.search(r"Discussion so far: (.+)\n", prompt).group(1))
        latest = turns[-1]["message"]
        self.turns.append(latest)
        self.files.extend(turns[-1].get("files", []))
        if latest.startswith("also"):
            plan = {**plan, "summary": plan["summary"] + " and document it"}
            answer = {"status": "revised", "plan": plan, "message": "Updated: also document it."}
        elif latest == "go ahead":
            answer = {"status": "converged", "plan": plan, "message": "Starting now."}
        else:
            answer = {"status": "answered", "plan": plan, "message": "Because scripts parse it."}
        path.write_text(json.dumps(answer), encoding="utf-8")
        return "turn done"


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(EXAMPLES, tmp_path / "wf")
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(v1_runtime.time, "sleep", lambda seconds: None)
    return tmp_path / "wf"


def _run(root: Path, slack: Slack, agent: Agent, monkeypatch, reviewed: list | None = None) -> dict:
    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(slack)
        return real(*args, **kwargs)

    monkeypatch.setattr(integrations.httpx, "Client", client)
    monkeypatch.setattr(local, "invoke", agent)
    config = root / WORKFLOW
    options = local.ExecutionOptions(
        credential_resolver=lambda reference: "xoxb-test-credential",
        execution_backend="outcomeci",
        policy_reviewer=lambda proposal: (
            (reviewed.append(proposal) if reviewed is not None else None)
            or {
                "decision": "allow",
                "proposal_sha256": proposal["proposal_sha256"],
                "reason": "ok",
            }
        ),
    )
    return debug.execute(
        root, config, compile_workflow(config), "webhook", _payload(), options, auto_continue=True
    )


def _consultation(root: Path, run_id: str) -> dict:
    return json.loads(
        (root / ".outcomeci/outcomes" / run_id / "discuss/consultation.json").read_text()
    )


def test_a_discussed_plan_becomes_one_pull_request_per_repository(workflow, monkeypatch):
    slack = Slack(["why --json?", "also document it", "go ahead"])
    agent = Agent()

    result = _run(workflow, slack, agent, monkeypatch)

    assert result["status"] == "completed"
    assert result["completed_phases"] == ["draft", "discuss", "implement", "announce"]
    consultation = _consultation(workflow, result["run_id"])
    assert consultation["status"] == "converged"
    assert consultation["current_version"] == 2
    assert consultation["versions"][1]["diff"] == {
        "added": [],
        "removed": [],
        "changed": ["summary"],
    }
    assert [turn["from"] for turn in consultation["turns"]] == ["agent"] + ["human", "agent"] * 3
    assert consultation["turns"][0]["message"] == "Plan: add --json"
    assert agent.turns == ["why --json?", "also document it", "go ahead"]
    # Every answer went into the plan's thread, in the request's channel.
    texts = [post["text"] for post in slack.posts]
    assert texts[0] == "Plan: add --json"
    assert texts[-1].startswith("PRs: ")
    texts = texts[1:-1]
    assert texts[0].startswith("Plan v1, as it will run:")
    assert texts[1:3] == ["Because scripts parse it.", "Updated: also document it."]
    assert texts[3].startswith("Plan v2, as it will run:")
    assert "Add --json output and document it" in texts[3]
    assert texts[4:] == ["Starting now."]
    assert {post["channel"] for post in slack.posts} == {"C0BUILD"}
    # One implement run per repository, each writing only its own repository.
    implements = [step for step in agent.steps if step["step"] == "implement"]
    assert [step["grants"][0]["args"]["repo"]["name"] for step in implements] == ["cli", "api"]
    assert {step["runner"] for step in implements} == {"claude"}
    assert slack.pulls == ["/repos/outcomeci/cli/pulls", "/repos/outcomeci/api/pulls"]
    assert len(agent.denials) == 2
    assert all("path must be under" in denial for denial in agent.denials)
    # The implement step received the converged plan, and announce the gathered PRs.
    plan_input = {item["name"]: item["value"] for item in implements[0]["inputs"]}["plan"]
    assert plan_input["summary"] == "Add --json output and document it"
    announce = next(step for step in agent.steps if step["step"] == "announce")
    prs = {item["name"]: item["value"] for item in announce["inputs"]}["pr"]
    assert [pr["number"] for pr in prs] == [1, 2]


def test_a_discussion_that_never_converges_is_capped_and_nothing_is_built(workflow, monkeypatch):
    slack = Slack(["why?"] * 20)
    agent = Agent()

    result = _run(workflow, slack, agent, monkeypatch)

    assert result["status"] == "completed"
    assert result["skipped_phases"] == ["implement", "announce"]
    consultation = _consultation(workflow, result["run_id"])
    assert consultation["status"] == "capped"
    assert len(consultation["turns"]) >= 12
    assert slack.pulls == []


def test_a_discussion_nobody_answers_times_out(workflow, monkeypatch):
    clock = iter(range(0, 10**7, 100_000))
    monkeypatch.setattr(v1_runtime.time, "monotonic", lambda: next(clock))
    slack = Slack([])

    result = _run(workflow, slack, Agent(), monkeypatch)

    assert _consultation(workflow, result["run_id"])["status"] == "timed_out"
    assert result["skipped_phases"] == ["implement", "announce"]
    outputs = json.loads(
        (workflow / ".outcomeci/outcomes" / result["run_id"] / "discuss/outputs.json").read_text()
    )
    assert outputs == {"plan": PLAN, "status": "timed_out"}


def test_a_screenshot_in_the_discussion_reaches_the_next_turn(workflow, monkeypatch):
    screenshot = {
        "text": "",
        "subtype": "file_share",
        "files": [{"id": "F0SHOT", "name": "error.png", "mimetype": "image/png"}],
    }
    slack = Slack([screenshot, "go ahead"])
    agent = Agent()

    result = _run(workflow, slack, agent, monkeypatch)

    assert result["status"] == "completed"
    (file,) = agent.files
    assert file["name"] == "error.png" and file["content_type"] == "image/png"
    assert Path(file["path"]).read_bytes() == b"\x89PNG"
    assert Path(file["path"]).is_relative_to(
        workflow / ".outcomeci/outcomes" / result["run_id"] / "attachments"
    )


def test_the_draft_step_opens_a_screenshot_attached_to_the_request(workflow, monkeypatch):
    slack = Slack(["go ahead"])
    agent = Agent()
    agent.open_request_files = True

    result = _run(workflow, slack, agent, monkeypatch)

    assert result["status"] == "completed"
    (opened,) = agent.opened
    assert opened["mimetype"] == "image/png"
    assert Path(opened["file"]["path"]).read_bytes() == b"\x89PNG"
    assert Path(opened["file"]["path"]).is_relative_to(
        workflow / ".outcomeci/outcomes" / result["run_id"] / "attachments"
    )


def test_the_reviewer_judges_a_step_against_its_approved_plan(workflow, monkeypatch):
    slack = Slack(["also document it", "go ahead"])
    reviewed: list = []

    result = _run(workflow, slack, Agent(), monkeypatch, reviewed)

    assert result["status"] == "completed"
    implement = [p for p in reviewed if str(p["request"].get("path", "")).startswith("/repos/")]
    assert implement, [p["request"] for p in reviewed]
    inputs = {item["name"]: item["value"] for item in implement[0]["context"]["inputs"]}
    # The discussion revised the plan; the reviewer sees the revision, not only the trigger.
    assert inputs["plan"]["summary"] == "Add --json output and document it"
    assert "context.inputs is what this step was given" in implement[0]["policy"]["content"]
