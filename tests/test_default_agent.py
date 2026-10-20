"""A workflow that names no default runner runs on the agent its environment
resolves: the one the workspace granted it (cloud) or the one this machine is
logged in to (local). Nothing implies codex."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from outcomeci.runtime import engine, launcher
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import ConfigError, compile_workflow

WORKFLOW = {
    "apiVersion": "outcomeci.workflow/v1",
    "name": "unnamed",
    "trigger": "manual",
    "steps": [
        {"draft": {"reason": "Draft it.", "from": "trigger", "returns": {"text": "string"}}},
        {
            "review": {
                "reason": "Review it.",
                "from": "draft",
                "using": {"agent": "claude"},
                "returns": {"ok": "boolean"},
            }
        },
    ],
}


def _compile(tmp_path: Path, document: dict) -> dict:
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(document))
    return compile_workflow(path)


def test_an_unnamed_default_compiles_to_no_runner(tmp_path: Path) -> None:
    compiled = _compile(tmp_path, WORKFLOW)
    assert compiled["workflow"]["spec"]["agents"]["default"] == {}
    steps = compiled["instructions"]["steps"]
    assert steps["draft"]["policy"]["runner"] is None
    assert steps["review"]["policy"]["runner"] == "claude"


def test_a_fallback_needs_no_named_default_to_differ_from(tmp_path: Path) -> None:
    document = {**WORKFLOW, "reasoning": {"fallback": [{"agent": "claude"}]}}
    compiled = _compile(tmp_path, document)
    assert compiled["workflow"]["spec"]["agents"]["default"]["fallback"]["runner"] == "claude"


def test_steps_on_the_unnamed_default_share_one_identity(tmp_path: Path) -> None:
    document = {
        **WORKFLOW,
        "secrets": {"a": "vault:agents/a", "b": "vault:agents/b"},
        "steps": [
            {"one": {"reason": "r", "using": {"auth": "secrets.a"}}},
            {"two": {"reason": "r", "using": {"auth": "secrets.b"}}},
        ],
    }
    with pytest.raises(ConfigError, match="one credential for the default runner"):
        _compile(tmp_path, document)


def test_the_engine_runs_an_unnamed_step_on_the_supplied_default() -> None:
    compiled = {"instructions": {"steps": {"draft": {"policy": {}}}}}
    assert engine._policy(compiled, "draft", None, None, "claude") == ("claude", None)
    # --agent still overrides, and a step's own runner wins over the default.
    assert engine._policy(compiled, "draft", "codex", None, "claude") == ("codex", None)
    named = {"instructions": {"steps": {"draft": {"policy": {"runner": "opencode"}}}}}
    assert engine._policy(named, "draft", None, "openrouter/x/y", "claude") == (
        "opencode",
        "openrouter/x/y",
    )
    with pytest.raises(ExecutionError, match="set reasoning.default.runner, or pass --agent"):
        engine._policy(compiled, "draft", None, None)


def test_the_launcher_leases_the_resolved_default_first(tmp_path: Path) -> None:
    compiled = _compile(tmp_path, WORKFLOW)
    assert launcher._needs_default_agent(compiled)
    assert launcher._runners(compiled, None, "codex") == ["codex", "claude"]
    assert launcher._runners(compiled, None, "claude") == ["claude"]
    assert launcher._runners(compiled, "opencode") == ["opencode"]
    with pytest.raises(ExecutionError, match="set reasoning.default.runner, or pass --agent"):
        launcher._runners(compiled, None)
    named = _compile(tmp_path, {**WORKFLOW, "reasoning": {"default": {"agent": "codex"}}})
    assert not launcher._needs_default_agent(named)
    assert launcher._runners(named, None, "claude") == ["codex", "claude"]


def _entry(provider: str, workflow_ids: list[str], **overrides) -> dict:
    entry = {
        "path": f"agents/{provider}",
        "provider": provider,
        "status": "active",
        "metadata": {"credential_type": "agent"},
        "workflow_ids": workflow_ids,
    }
    entry.update(overrides)
    return entry


def test_a_cloud_run_resolves_the_one_agent_granted_to_the_workflow(monkeypatch) -> None:
    listings = {}
    monkeypatch.setattr(
        "outcomeci.cloud.vault_request", lambda workspace_id, operation: listings[operation]
    )
    listings["list"] = {
        "entries": [
            _entry("claude", ["wf-1"]),
            _entry("codex", ["wf-2"]),  # granted to another workflow
            _entry("opencode", ["wf-1"], status="revoked"),
            _entry("codex", ["wf-1"], path="agents/codex-work"),  # a named identity
        ]
    }
    assert launcher._granted_default_agent("ws", "wf-1") == "claude"

    listings["list"] = {"entries": [_entry("codex", ["wf-2"])]}
    with pytest.raises(ExecutionError, match="no agent is connected and granted"):
        launcher._granted_default_agent("ws", "wf-1")

    listings["list"] = {"entries": [_entry("codex", ["wf-1"]), _entry("claude", ["wf-1"])]}
    with pytest.raises(ExecutionError, match=r"several agents .*\(claude, codex\)"):
        launcher._granted_default_agent("ws", "wf-1")


def test_a_local_run_resolves_the_one_login_on_this_machine(monkeypatch, tmp_path) -> None:
    logins = {"claude": {"provider": "claude", "credential": "t"}}

    def local_login(root, provider, credential=None):
        if provider not in logins:
            raise ExecutionError(f"no {provider} login")
        return logins[provider]

    monkeypatch.setattr(launcher, "_local_login", local_login)
    assert launcher._machine_default_agent(tmp_path) == "claude"

    logins.clear()
    with pytest.raises(ExecutionError, match="no agent login on this machine"):
        launcher._machine_default_agent(tmp_path)

    logins.update(codex={}, claude={})
    with pytest.raises(ExecutionError, match=r"several agents \(claude, codex\)"):
        launcher._machine_default_agent(tmp_path)
