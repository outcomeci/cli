from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
import pytest
import yaml

from outcomeci import local
from outcomeci.config import ConfigError, _human_interactions, compile_workflow
from outcomeci.integrations import IntegrationExecutor as RealExecutor
from outcomeci.process import ExecutionError


def _hook(**overrides):
    hook = {
        "id": "plan_consultation",
        "participant": "requester",
        "purpose": "Discuss and refine the proposed plan",
        "interaction": "consultation",
        "delivery": {
            "type": "slack",
            "mode": "reply",
            "source": "plan.outputs.delivery",
        },
    }
    hook.update(overrides)
    return hook


def test_valid_reply_delivery_is_normalized():
    result = _human_interactions({"before": [_hook()]}, "spec.agents.phases.implement.humans")
    normalized = result["before"][0]
    assert normalized["delivery"] == {
        "type": "slack",
        "mode": "reply",
        "source": "plan.outputs.delivery",
        "poll_interval_seconds": 20,
    }
    assert normalized["on_timeout"] == "fail"
    assert normalized["wait"] == {"strategy": "block", "timeout_seconds": 300}


def test_reply_delivery_requires_consultation_interaction():
    with pytest.raises(ConfigError, match="requires interaction: consultation"):
        _human_interactions(
            {"before": [_hook(interaction="approval")]}, "spec.agents.phases.implement.humans"
        )


def test_reply_delivery_only_supported_before():
    with pytest.raises(ConfigError, match="only supported for timing: before"):
        _human_interactions({"during": [_hook()]}, "spec.agents.phases.implement.humans")
    with pytest.raises(ConfigError, match="only supported for timing: before"):
        _human_interactions({"after": [_hook()]}, "spec.agents.phases.implement.humans")


def test_reply_delivery_requires_source():
    hook = _hook(delivery={"type": "slack", "mode": "reply"})
    with pytest.raises(ConfigError, match="delivery.source"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.implement.humans")


def test_reply_delivery_rejects_malformed_source():
    hook = _hook(delivery={"type": "slack", "mode": "reply", "source": "not-a-reference"})
    with pytest.raises(ConfigError, match="delivery.source"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.implement.humans")


def test_reply_delivery_rejects_unknown_fields():
    hook = _hook(
        delivery={
            "type": "slack",
            "mode": "reply",
            "source": "plan.outputs.delivery",
            "emoji": "+1",
        }
    )
    with pytest.raises(ConfigError, match="unknown fields"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.implement.humans")


def test_reply_delivery_poll_interval_bounds():
    hook = _hook(
        delivery={
            "type": "slack",
            "mode": "reply",
            "source": "plan.outputs.delivery",
            "poll_interval_seconds": 1000,
        }
    )
    with pytest.raises(ConfigError, match="poll_interval_seconds"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.implement.humans")


def test_on_timeout_accepts_fail_and_continue_for_reply():
    for value in ("fail", "continue"):
        result = _human_interactions(
            {"before": [_hook(on_timeout=value)]}, "spec.agents.phases.implement.humans"
        )
        assert result["before"][0]["on_timeout"] == value


def test_reply_delivery_requires_block_wait_strategy():
    hook = _hook(wait={"strategy": "ask", "timeout_seconds": 300})
    with pytest.raises(ConfigError, match="wait.strategy: block"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.implement.humans")


def test_reply_delivery_defaults_timeout_when_wait_partially_set():
    result = _human_interactions(
        {"before": [_hook(wait={"strategy": "block"})]}, "spec.agents.phases.implement.humans"
    )
    assert result["before"][0]["wait"] == {"strategy": "block", "timeout_seconds": 300}


def _workflow(tmp_path: Path, *, on_timeout: str = "fail") -> Path:
    instructions = tmp_path / ".outcomeci" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (instructions / "implement.md").write_text("# Implement\n", encoding="utf-8")
    value = {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "reply-gate"},
        "spec": {
            "triggers": {"manual": {"type": "manual"}},
            "backend": {"provider": "filesystem"},
            "context": {"provider": "filesystem", "include": []},
            "instructions": {"standup": {"path": ".outcomeci/instructions/plan.md"}},
            "agents": {
                "default": {"runner": "codex"},
                "phases": {
                    "plan": {
                        "instructions": ".outcomeci/instructions/plan.md",
                        "needs": [],
                        "expects": {
                            "outputs": [
                                {
                                    "name": "delivery",
                                    "path": "delivery.json",
                                    "media_type": "application/json",
                                }
                            ]
                        },
                    },
                    "implement": {
                        "instructions": ".outcomeci/instructions/implement.md",
                        "needs": ["plan"],
                        "integrations": [
                            {
                                "type": "human",
                                "timing": "before",
                                "id": "plan_consultation",
                                "participant": "requester",
                                "purpose": "Discuss and refine the proposed plan",
                                "interaction": "consultation",
                                "delivery": {
                                    "type": "slack",
                                    "mode": "reply",
                                    "source": "plan.outputs.delivery",
                                    "poll_interval_seconds": 5,
                                },
                                "on_timeout": on_timeout,
                                "wait": {"strategy": "block", "timeout_seconds": 12},
                            },
                            {"type": "api", "capability": "slack.get_replies"},
                        ],
                    },
                },
            },
            "connections": {
                "slack": {
                    "provider": "http",
                    "base_url": "https://slack.com",
                    "auth": {"type": "bearer", "credential": "env:SLACK_TOKEN"},
                }
            },
            "integrations": {
                "slack": {
                    "connection": "slack",
                    "access": {"mode": "schema"},
                    "operations": {
                        "get_replies": {
                            "description": "Check replies on a message's thread.",
                            "input": {
                                "type": "object",
                                "required": ["channel", "timestamp"],
                                "properties": {
                                    "channel": {"type": "string"},
                                    "timestamp": {"type": "string"},
                                },
                                "additionalProperties": False,
                            },
                            "request": {
                                "method": "GET",
                                "path": "/api/conversations.replies",
                                "query": {
                                    "channel": "{{ input.channel }}",
                                    "timestamp": "{{ input.timestamp }}",
                                },
                            },
                            "response": {"expose": {"messages": "body.messages"}},
                        }
                    },
                }
            },
        },
    }
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_compiles_cleanly_with_a_valid_reply_hook(tmp_path: Path):
    compiled = compile_workflow(_workflow(tmp_path))
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    assert hook["delivery"]["type"] == "slack"
    assert hook["delivery"]["mode"] == "reply"
    assert "slack.get_replies" in compiled["instructions"]["phases"]["implement"]["capabilities"]


def _run_state(run_id: str = "run-1") -> dict:
    return {"run_id": run_id, "phase": "implement"}


def _write_delivery(root: Path, run_id: str, *, channel="C123", ts="1700000000.000100") -> None:
    outcome_root = root / ".outcomeci" / "outcomes" / run_id
    outcome_root.mkdir(parents=True, exist_ok=True)
    (outcome_root / "delivery.json").write_text(
        json.dumps({"delivered": True, "channel": channel, "ts": ts, "status": "ok"}),
        encoding="utf-8",
    )


def _replies_response(messages: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "messages": messages})

    return handler


_ROOT_MESSAGE = {"text": "Proposed plan...", "ts": "1700000000.000100"}


def test_resolve_reply_resolves_on_first_new_message(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(
                _replies_response(
                    [_ROOT_MESSAGE, {"text": "looks good, approved", "ts": "1700000010.0"}]
                )
            ),
        ),
    )
    state = _run_state()
    local._resolve_reply(tmp_path, config, state, "implement", "before", hook, lambda ref: "token")
    request = json.loads(
        (
            tmp_path / ".outcomeci/outcomes/run-1/interactions/implement/plan_consultation.json"
        ).read_text()
    )
    assert request["status"] == "responded"
    assert request["response"]["message"] == "looks good, approved"
    assert state["interaction_history"][0]["status"] == "responded"


def test_resolve_reply_ignores_the_root_message_and_bot_replies(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    responses = [
        _replies_response([_ROOT_MESSAGE, {"text": "bot echo", "ts": "1.0", "bot_id": "B1"}]),
        _replies_response(
            [
                _ROOT_MESSAGE,
                {"text": "bot echo", "ts": "1.0", "bot_id": "B1"},
                {"text": "go ahead", "ts": "2.0"},
            ]
        ),
    ]
    calls = iter(responses)

    def transport(request: httpx.Request) -> httpx.Response:
        return next(calls)(request)

    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(transport),
        ),
    )
    monkeypatch.setattr(local.time, "sleep", lambda _seconds: None)
    state = _run_state()
    local._resolve_reply(tmp_path, config, state, "implement", "before", hook, lambda ref: "token")
    request = json.loads(
        (
            tmp_path / ".outcomeci/outcomes/run-1/interactions/implement/plan_consultation.json"
        ).read_text()
    )
    assert request["response"]["message"] == "go ahead"


def test_resolve_reply_raises_when_no_credential_resolver(tmp_path: Path):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    with pytest.raises(ExecutionError, match="credential resolver"):
        local._resolve_reply(tmp_path, config, _run_state(), "implement", "before", hook, None)


def test_resolve_reply_raises_on_missing_source_file(tmp_path: Path):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    with pytest.raises(ExecutionError, match="could not be read"):
        local._resolve_reply(
            tmp_path, config, _run_state(), "implement", "before", hook, lambda ref: "token"
        )


def test_resolve_reply_times_out_and_fails(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path, on_timeout="fail")
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    monkeypatch.setattr(local.time, "sleep", lambda _seconds: None)
    ticks = itertools.chain([0, 1, 2, 3, 4], itertools.repeat(400))
    monkeypatch.setattr(local.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(_replies_response([_ROOT_MESSAGE])),
        ),
    )
    with pytest.raises(ExecutionError, match="consultation window expired"):
        local._resolve_reply(
            tmp_path, config, _run_state(), "implement", "before", hook, lambda ref: "token"
        )


def test_resolve_reply_times_out_and_continues(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path, on_timeout="continue")
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["implement"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    monkeypatch.setattr(local.time, "sleep", lambda _seconds: None)
    ticks = itertools.chain([0, 1, 2, 3, 4], itertools.repeat(400))
    monkeypatch.setattr(local.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(_replies_response([_ROOT_MESSAGE])),
        ),
    )
    state = _run_state()
    local._resolve_reply(tmp_path, config, state, "implement", "before", hook, lambda ref: "token")
    request = json.loads(
        (
            tmp_path / ".outcomeci/outcomes/run-1/interactions/implement/plan_consultation.json"
        ).read_text()
    )
    assert request["status"] == "answered"
    assert request["response"]["message"] == "Consultation window expired with no reply"


def _awaiting_continuation(root: Path, run_id: str = "run-1") -> None:
    local._write(
        root,
        {
            "run_id": run_id,
            "intent": "gate the second phase",
            "phase": "plan",
            "status": "awaiting_confirmation",
            "completed_phases": ["plan"],
        },
    )


def test_continue_run_resolves_the_before_hook_of_a_reply_gated_phase(tmp_path: Path, monkeypatch):
    """A resolved reply hook (status: responded) must actually unblock the
    gated phase -- _first_required_interaction is the one place that decides
    whether a required interaction has been satisfied, and reply's
    status="responded" needs to be recognized there just as reaction's
    status="approved" already is."""
    config = _workflow(tmp_path)
    _write_delivery(tmp_path, "run-1")
    _awaiting_continuation(tmp_path)
    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(
                _replies_response([_ROOT_MESSAGE, {"text": "approved", "ts": "2.0"}])
            ),
        ),
    )
    executed = []
    monkeypatch.setattr(
        local,
        "_execute",
        lambda root, config, state, **kwargs: (
            executed.append([item["status"] for item in state.get("interaction_history", [])])
            or state
        ),
    )
    local.continue_run(
        tmp_path,
        config,
        "run-1",
        approve=True,
        options=local.ExecutionOptions(credential_resolver=lambda ref: "token"),
    )
    assert executed == [["responded"]]
