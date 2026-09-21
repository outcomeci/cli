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
        "id": "sentry_reaction_approval",
        "participant": "approver",
        "purpose": "Wait for a thumbs up",
        "interaction": "approval",
        "delivery": {"type": "reaction", "source": "notify.outputs.delivery", "emoji": "+1"},
    }
    hook.update(overrides)
    return hook


def test_valid_reaction_delivery_is_normalized():
    result = _human_interactions({"before": [_hook()]}, "spec.agents.phases.approve.humans")
    normalized = result["before"][0]
    assert normalized["delivery"] == {
        "type": "reaction",
        "source": "notify.outputs.delivery",
        "emoji": "+1",
        "poll_interval_seconds": 20,
    }
    assert normalized["on_timeout"] == "fail"
    assert normalized["wait"] == {"strategy": "block", "timeout_seconds": 300}


def test_reaction_delivery_requires_approval_interaction():
    with pytest.raises(ConfigError, match="requires interaction: approval"):
        _human_interactions(
            {"before": [_hook(interaction="review")]}, "spec.agents.phases.approve.humans"
        )


def test_reaction_delivery_only_supported_before():
    with pytest.raises(ConfigError, match="only supported for timing: before"):
        _human_interactions({"after": [_hook()]}, "spec.agents.phases.approve.humans")


def test_reaction_delivery_requires_source():
    hook = _hook(delivery={"type": "reaction"})
    with pytest.raises(ConfigError, match="delivery.source"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.approve.humans")


def test_reaction_delivery_rejects_malformed_source():
    hook = _hook(delivery={"type": "reaction", "source": "not-a-reference"})
    with pytest.raises(ConfigError, match="delivery.source"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.approve.humans")


def test_reaction_delivery_rejects_unknown_fields():
    hook = _hook(delivery={"type": "reaction", "source": "notify.outputs.delivery", "bogus": "x"})
    with pytest.raises(ConfigError, match="unknown fields"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.approve.humans")


def test_reaction_delivery_poll_interval_bounds():
    hook = _hook(
        delivery={
            "type": "reaction",
            "source": "notify.outputs.delivery",
            "poll_interval_seconds": 1,
        }
    )
    with pytest.raises(ConfigError, match="poll_interval_seconds"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.approve.humans")


def test_on_timeout_accepts_fail_and_continue():
    for value in ("fail", "continue"):
        result = _human_interactions(
            {"before": [_hook(on_timeout=value)]}, "spec.agents.phases.approve.humans"
        )
        assert result["before"][0]["on_timeout"] == value


def test_on_timeout_rejects_invalid_value():
    with pytest.raises(ConfigError, match="on_timeout is unsupported"):
        _human_interactions(
            {"before": [_hook(on_timeout="retry")]}, "spec.agents.phases.approve.humans"
        )


def test_on_timeout_rejected_without_reaction_delivery():
    hook = _hook(delivery={"type": "local"}, on_timeout="fail")
    with pytest.raises(ConfigError, match="only supported with delivery.type: reaction"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.approve.humans")


def test_reaction_delivery_requires_block_wait_strategy():
    hook = _hook(wait={"strategy": "ask"})
    with pytest.raises(ConfigError, match="wait.strategy: block"):
        _human_interactions({"before": [hook]}, "spec.agents.phases.approve.humans")


def test_reaction_delivery_defaults_timeout_when_wait_partially_set():
    result = _human_interactions(
        {"before": [_hook(wait={"strategy": "block"})]}, "spec.agents.phases.approve.humans"
    )
    assert result["before"][0]["wait"] == {"strategy": "block", "timeout_seconds": 300}


def _workflow(tmp_path: Path, *, on_timeout: str = "fail") -> Path:
    instructions = tmp_path / ".outcomeci" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "notify.md").write_text("# Notify\n", encoding="utf-8")
    (instructions / "approve.md").write_text("# Approve\n", encoding="utf-8")
    value = {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "reaction-gate"},
        "spec": {
            "triggers": {"manual": {"type": "manual"}},
            "backend": {"provider": "filesystem"},
            "context": {"provider": "filesystem", "include": []},
            "instructions": {"standup": {"path": ".outcomeci/instructions/notify.md"}},
            "agents": {
                "default": {"runner": "codex"},
                "phases": {
                    "notify": {
                        "instructions": ".outcomeci/instructions/notify.md",
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
                    "approve": {
                        "instructions": ".outcomeci/instructions/approve.md",
                        "needs": ["notify"],
                        "integrations": [
                            {
                                "type": "human",
                                "timing": "before",
                                "id": "sentry_reaction_approval",
                                "participant": "approver",
                                "purpose": "Wait for a thumbs up",
                                "interaction": "approval",
                                "delivery": {
                                    "type": "reaction",
                                    "source": "notify.outputs.delivery",
                                    "emoji": "+1",
                                    "poll_interval_seconds": 5,
                                },
                                "on_timeout": on_timeout,
                                "wait": {"strategy": "block", "timeout_seconds": 12},
                            },
                            {"type": "api", "capability": "slack.get_reactions"},
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
                        "get_reactions": {
                            "description": "Check reactions on a message.",
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
                                "path": "/api/reactions.get",
                                "query": {
                                    "channel": "{{ input.channel }}",
                                    "timestamp": "{{ input.timestamp }}",
                                },
                            },
                            "response": {"expose": {"reactions": "body.message.reactions"}},
                        }
                    },
                }
            },
        },
    }
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_source_must_reference_a_declared_output(tmp_path: Path):
    config = _workflow(tmp_path)
    value = yaml.safe_load(config.read_text())
    value["spec"]["agents"]["phases"]["approve"]["integrations"][0]["delivery"]["source"] = (
        "notify.outputs.missing"
    )
    config.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="references unknown output"):
        compile_workflow(config)


def test_source_must_come_from_a_direct_dependency(tmp_path: Path):
    config = _workflow(tmp_path)
    value = yaml.safe_load(config.read_text())
    value["spec"]["agents"]["phases"]["approve"]["needs"] = []
    config.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="direct dependency"):
        compile_workflow(config)


def test_compiles_cleanly_with_a_valid_reaction_hook(tmp_path: Path):
    compiled = compile_workflow(_workflow(tmp_path))
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
    assert hook["delivery"]["type"] == "reaction"
    assert "slack.get_reactions" in compiled["instructions"]["phases"]["approve"]["capabilities"]


def _run_state(run_id: str = "run-1") -> dict:
    return {"run_id": run_id, "phase": "approve"}


def _write_delivery(root: Path, run_id: str, *, channel="C123", ts="1700000000.000100") -> None:
    outcome_root = root / ".outcomeci" / "outcomes" / run_id
    outcome_root.mkdir(parents=True, exist_ok=True)
    (outcome_root / "delivery.json").write_text(
        json.dumps({"delivered": True, "channel": channel, "ts": ts, "status": "ok"}),
        encoding="utf-8",
    )


def _reactions_response(names: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message": {"reactions": [{"name": name, "count": 1} for name in names]},
            },
        )

    return handler


def test_resolve_reaction_approves_on_first_poll(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(_reactions_response(["+1"])),
        ),
    )
    state = _run_state()
    local._resolve_reaction(tmp_path, config, state, "approve", "before", hook, lambda ref: "token")
    request = json.loads(
        (
            tmp_path
            / ".outcomeci/outcomes/run-1/interactions/approve/sentry_reaction_approval.json"
        ).read_text()
    )
    assert request["status"] == "approved"
    assert state["interaction_history"][0]["status"] == "approved"


def test_resolve_reaction_raises_when_no_credential_resolver(tmp_path: Path):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")
    with pytest.raises(ExecutionError, match="credential resolver"):
        local._resolve_reaction(tmp_path, config, _run_state(), "approve", "before", hook, None)


def test_resolve_reaction_raises_on_missing_source_file(tmp_path: Path):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
    with pytest.raises(ExecutionError, match="could not be read"):
        local._resolve_reaction(
            tmp_path, config, _run_state(), "approve", "before", hook, lambda ref: "token"
        )


def test_resolve_reaction_times_out_and_fails(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path, on_timeout="fail")
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
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
            transport=httpx.MockTransport(_reactions_response([])),
        ),
    )
    with pytest.raises(ExecutionError, match="approval window expired"):
        local._resolve_reaction(
            tmp_path, config, _run_state(), "approve", "before", hook, lambda ref: "token"
        )


def test_resolve_reaction_times_out_and_continues(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path, on_timeout="continue")
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
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
            transport=httpx.MockTransport(_reactions_response([])),
        ),
    )
    state = _run_state()
    local._resolve_reaction(tmp_path, config, state, "approve", "before", hook, lambda ref: "token")
    assert state["interaction_history"][0]["status"] == "answered"


def test_open_interaction_returns_none_for_resolved_reaction_hook(tmp_path: Path, monkeypatch):
    config = _workflow(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["approve"]["humans"]["before"][0]
    _write_delivery(tmp_path, "run-1")

    monkeypatch.setattr(
        local,
        "IntegrationExecutor",
        lambda compiled, **kwargs: RealExecutor(
            compiled,
            resolver=kwargs["resolver"],
            reviewed=kwargs["reviewed"],
            transport=httpx.MockTransport(_reactions_response(["+1"])),
        ),
    )
    state = _run_state()
    result = local._open_interaction(
        tmp_path,
        state,
        "approve",
        "before",
        hook,
        config=config,
        credential_resolver=lambda ref: "token",
    )
    assert result is None


def test_open_interaction_unaffected_for_local_delivery(tmp_path: Path):
    definition = {
        "id": "confirm",
        "participant": {"role": "approver"},
        "purpose": "Confirm",
        "interaction": "approval",
        "required": True,
        "delivery": {"type": "local"},
        "wait": {"strategy": "ask"},
    }
    state = {"run_id": "run-2"}
    result = local._open_interaction(tmp_path, state, "approve", "before", definition)
    assert result is state
    assert result["status"] == "awaiting_input"
