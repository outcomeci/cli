"""Model steps: reasoning profiles without a runner, run as tool loops through the broker."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest
import test_v1_sentry as sentry
import test_v1_slack as slack_example
import yaml

from outcomeci import integrations, local, models, run_container, v1_runtime
from outcomeci.config import ConfigError, compile_workflow
from outcomeci.integrations import IntegrationExecutor
from outcomeci.policy import PolicyExecutor
from outcomeci.process import ExecutionError

TRIAGE = {
    "decision": "fix",
    "issue": {
        "title": "KeyError: 'plan'",
        "message": "KeyError: 'plan'",
        "culprit": "app.billing",
        "level": "error",
        "project": "cli",
        "url": "https://sentry.io/issues/1",
    },
    "repo": {"owner": "outcomeci", "name": "cli"},
    "reason": "billing code bug",
}


def _profiles(path: Path, steps: dict[str, str], extra: dict | None = None) -> None:
    """Give a workflow a `light` model profile and point `steps` at profiles."""
    document = yaml.safe_load(path.read_text())
    document["secrets"]["anthropic"] = "vault:anthropic/api-key"
    document["reasoning"]["light"] = {"model": "anthropic/claude-haiku-4-5"}
    document["reasoning"].update(extra or {})
    for entry in document["steps"]:
        name, step = next(iter(entry.items()))
        if name in steps:
            step["using"] = steps[name]
    path.write_text(yaml.safe_dump(document, sort_keys=False))


class Model:
    """A scripted model: each step's turns in order, recording what it was sent."""

    def __init__(self, script: dict[str, list]):
        self.script = {step: list(turns) for step, turns in script.items()}
        self.calls: list[dict] = []

    def __call__(self, *, step, profile, messages, tools):
        self.calls.append(
            {"step": step, "profile": profile, "messages": json.loads(json.dumps(messages))}
        )
        turn = self.script[step].pop(0)
        if callable(turn):
            turn = turn(messages)
        return {"message": {"content": turn.get("text"), "tool_calls": turn.get("calls", [])}}


def _call(name: str, arguments: dict, index: int = 1) -> dict:
    return {"id": f"call_{index}", "name": name, "arguments": json.dumps(arguments)}


def _sentry_run(root: Path, model: Model, agent, monkeypatch, reviews: list | None = None):
    monkeypatch.setattr(local, "invoke", agent)
    config = root / sentry.WORKFLOW

    def review(proposal):
        if reviews is not None:
            reviews.append(proposal)
        return {"decision": "allow", "proposal_sha256": proposal["proposal_sha256"], "reason": "ok"}

    options = local.ExecutionOptions(
        credential_resolver=lambda reference: "xoxb-or-ghp-token",
        policy_reviewer=review,
        model_client=model,
    )
    return run_container.execute(
        root,
        config,
        compile_workflow(config),
        "webhook",
        sentry._payload(),
        options,
        auto_continue=True,
    )


@pytest.fixture
def sentry_workflow(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(sentry.EXAMPLES, tmp_path / "wf")
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(v1_runtime.time, "sleep", lambda seconds: None)
    return tmp_path / "wf"


def test_a_model_step_calls_its_granted_tools_through_the_broker(sentry_workflow, monkeypatch):
    _profiles(sentry_workflow / sentry.WORKFLOW, {"triage": "light", "announce": "light"})
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    agent = sentry.Agent()
    model = Model(
        {
            "triage": [
                {
                    "calls": [
                        _call("slack__post", {"channel": "general", "text": "wrong channel"}, 1),
                        _call("slack__post", {"text": "*Sentry alert -- KeyError: 'plan'*"}, 2),
                    ]
                },
                {"calls": [_call("return_result", TRIAGE, 3)]},
            ],
            "announce": [
                {"calls": [_call("slack__post", {"text": "PR opened"}, 1)]},
                {"text": "Posted the PR link."},
            ],
        }
    )

    result = _sentry_run(sentry_workflow, model, agent, monkeypatch)

    assert result["status"] == "completed"
    # Only the fix step started an agent; triage and announce were model calls.
    assert list(agent.prompts) == ["fix"]
    assert [call["profile"] for call in model.calls] == ["light"] * 4
    # The grant held: the call to another channel came back to the model as an error.
    refused = json.loads(model.calls[1]["messages"][3]["content"])
    assert "error" in refused and "channel" in refused["error"]
    posts = [
        json.loads(item.content) for item in services.sent("slack.com", "/api/chat.postMessage")
    ]
    assert posts[0]["channel"] == "sentry" and posts[0]["text"].startswith("*Sentry alert")
    assert posts[-1]["text"] == "PR opened" and posts[-1]["thread_ts"] == "17.1"
    outputs = json.loads(
        (
            sentry_workflow / ".outcomeci/outcomes" / result["run_id"] / "triage/outputs.json"
        ).read_text()
    )
    assert outputs["decision"] == "fix" and outputs["repo"]["name"] == "cli"


def test_an_invalid_result_goes_back_to_the_model(sentry_workflow, monkeypatch):
    _profiles(sentry_workflow / sentry.WORKFLOW, {"triage": "light"})
    sentry._serve(monkeypatch, sentry.Services())
    model = Model(
        {
            "triage": [
                {"calls": [_call("return_result", {"decision": "maybe"})]},
                {"calls": [_call("return_result", {**TRIAGE, "decision": "no_op"}, 2)]},
            ]
        }
    )

    result = _sentry_run(sentry_workflow, model, sentry.Agent(), monkeypatch)

    assert result["completed_steps"][0] == "triage"
    problem = json.loads(model.calls[1]["messages"][-1]["content"])
    assert problem["error"].startswith("result is invalid")


def test_a_model_that_never_returns_fails_the_step():
    model = Model({"s": [{"text": "done"}, {"text": "still done"}]})

    with pytest.raises(ExecutionError, match="without calling return_result"):
        models.run(
            model,
            step="s",
            profile="light",
            system="x",
            user="y",
            capabilities=[],
            call=lambda capability, inputs: {},
            returns={"type": "object"},
        )
    assert model.calls[1]["messages"][-1]["content"].startswith("Call return_result")


def test_tool_calls_are_capped():
    looping = {"calls": [_call("github__read", {"method": "GET", "path": "/x"})]}
    model = Model({"s": [looping] * (models.MAX_TOOL_CALLS + 1)})

    with pytest.raises(ExecutionError, match="more than 20 tool calls"):
        models.run(
            model,
            step="s",
            profile="light",
            system="x",
            user="y",
            capabilities=[{"capability": "github.read", "input": {"type": "object"}}],
            call=lambda capability, inputs: {"ok": True},
        )


def test_a_discussion_turn_can_be_a_model(monkeypatch, tmp_path):
    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    root = tmp_path / "wf"
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(v1_runtime.time, "sleep", lambda seconds: None)
    _profiles(root / slack_example.WORKFLOW, {"discuss": "light"})

    def approve(messages):
        text = messages[1]["content"]
        plan = json.loads(re.search(r"Current plan \(version \d+\): (.+)\n", text).group(1))
        return {
            "calls": [
                _call("return_result", {"status": "converged", "plan": plan, "message": "On it."})
            ]
        }

    model = Model({"discuss": [approve]})
    slack = slack_example.Slack(["go ahead"])
    real = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(slack)}),
    )
    agent = slack_example.Agent()
    monkeypatch.setattr(local, "invoke", agent)
    config = root / slack_example.WORKFLOW
    options = local.ExecutionOptions(
        credential_resolver=lambda reference: "xoxb-test-credential",
        policy_reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
        model_client=model,
    )

    result = run_container.execute(
        root,
        config,
        compile_workflow(config),
        "webhook",
        slack_example._payload(),
        options,
        auto_continue=True,
    )

    assert result["status"] == "completed"
    assert agent.turns == []  # no agent took a turn
    assert "return_result" in model.calls[0]["messages"][1]["content"]
    assert any(post["text"] == "On it." for post in slack.posts)


def test_the_review_profile_decides_a_local_review(tmp_path, monkeypatch):
    config = tmp_path / sentry.WORKFLOW
    shutil.copytree(sentry.EXAMPLES, tmp_path, dirs_exist_ok=True)
    _profiles(
        config, {}, {"review": {"model": "anthropic/claude-sonnet-5", "key": "secrets.anthropic"}}
    )
    compiled = compile_workflow(config)
    sent: list = []

    def completion(**kwargs):
        sent.append(kwargs)
        call = type(
            "Call",
            (),
            {
                "id": "c1",
                "function": type(
                    "F",
                    (),
                    {
                        "name": "return_result",
                        "arguments": json.dumps({"decision": "deny", "reason": "too broad"}),
                    },
                )(),
            },
        )()
        message = type("M", (), {"content": None, "tool_calls": [call]})()
        return type(
            "R",
            (),
            {"choices": [type("C", (), {"message": message, "finish_reason": "tool_calls"})()]},
        )()

    import litellm

    monkeypatch.setattr(litellm, "completion", completion)
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda reference: {"secrets": {"api_key": f"key-for:{reference}"}},
    )
    reviewer = PolicyExecutor(executor, tmp_path / "broker", {})

    decision = reviewer._review(
        {"proposal_sha256": "a" * 64, "policy": {"content": "Only one PR."}, "request": {}}
    )

    assert decision == {"decision": "deny", "reason": "too broad", "proposal_sha256": "a" * 64}
    assert sent[0]["model"] == "anthropic/claude-sonnet-5"
    assert sent[0]["api_key"] == "key-for:vault:anthropic/api-key"


def test_a_platform_funded_local_profile_reads_the_provider_env_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert models._key({"model": "anthropic/claude-haiku-4-5"}, None) == "sk-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ExecutionError, match="ANTHROPIC_API_KEY"):
        models._key({"model": "anthropic/claude-haiku-4-5"}, None)


@pytest.mark.parametrize(
    ("reasoning", "using", "message"),
    [
        ({"review": {"runner": "claude"}}, None, "review is a model"),
        ({"light": {"model": "mistral/large"}}, None, "<provider>/<model>"),
        ({"light": {"model": "anthropic/x", "key": "secrets.missing"}}, None, "declared secret"),
        ({"light": {"model": "anthropic/x", "temperature": 1}}, None, "supports model and key"),
        ({}, "light", "reasoning profile"),
        ({"review": {"model": "anthropic/x"}}, "review", "other than review"),
    ],
)
def test_profiles_are_checked_when_the_workflow_compiles(tmp_path, reasoning, using, message):
    shutil.copytree(sentry.EXAMPLES, tmp_path, dirs_exist_ok=True)
    path = tmp_path / sentry.WORKFLOW
    document = yaml.safe_load(path.read_text())
    document["reasoning"].update(reasoning)
    if using:
        document["steps"][0]["triage"]["using"] = using
    path.write_text(yaml.safe_dump(document, sort_keys=False))

    with pytest.raises(ConfigError, match=re.escape(message)):
        compile_workflow(path)


def test_a_named_agent_profile_sets_the_steps_runner(tmp_path):
    shutil.copytree(sentry.EXAMPLES, tmp_path, dirs_exist_ok=True)
    path = tmp_path / sentry.WORKFLOW
    _profiles(path, {"fix": "deep"}, {"deep": {"runner": "claude", "model": "claude-opus-5-5"}})

    compiled = compile_workflow(path)

    assert compiled["instructions"]["steps"]["fix"]["policy"] == {
        "runner": "claude",
        "model": "claude-opus-5-5",
    }
    assert "reasoning" not in compiled["instructions"]["steps"]["fix"]["v1"]


def test_a_step_shows_the_model_at_most_three_small_images(tmp_path):
    files = []
    for index in range(5):
        path = tmp_path / f"shot{index}.png"
        path.write_bytes(b"\x89PNG" + b"0" * (10 if index != 1 else models.IMAGE_LIMIT))
        files.append({"path": str(path), "content_type": "image/png"})
    files.append({"path": str(tmp_path / "notes.txt"), "content_type": "text/plain"})

    parts = models.image_parts(files)

    assert len(parts) == models.MAX_IMAGES
    assert all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in parts)
    # The oversized second image was skipped, not counted.
    assert models.image_parts(files[1:2]) == []
