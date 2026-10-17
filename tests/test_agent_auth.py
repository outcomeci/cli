"""Agent syntax aliases and named Vault identity selection."""

import json

import pytest
import yaml

from outcomeci.runtime import launcher
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import ConfigError, compile_workflow


def compile_doc(tmp_path, reasoning, steps=None):
    path = tmp_path / "outcome.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v1",
                "trigger": "manual",
                "secrets": {
                    "coder": "vault:agents/team",
                    "other": "vault:agents/other",
                    "model": "vault:models/review",
                },
                "reasoning": reasoning,
                "steps": steps or [{"work": {"reason": "Do the work."}}],
            }
        )
    )
    return compile_workflow(path)


def test_agent_auth_resolves_and_inherits(tmp_path):
    compiled = compile_doc(tmp_path, {"default": {"agent": "codex", "auth": "secrets.coder"}})
    assert compiled["instructions"]["steps"]["work"]["policy"]["credential"] == "vault:agents/team"
    assert launcher._agent_credentials(compiled) == {"codex": "vault:agents/team"}
    assert "vault:agents/team" not in launcher._vault_references(compiled)


@pytest.mark.parametrize(
    "profile",
    [
        {"agent": "codex", "runner": "codex"},
        {"agent": "codex", "auth": "secrets.missing"},
    ],
)
def test_invalid_agent_auth(tmp_path, profile):
    with pytest.raises(ConfigError):
        compile_doc(tmp_path, {"default": profile})


def test_model_auth_alias_and_fallback(tmp_path):
    compiled = compile_doc(
        tmp_path,
        {
            "reviewer": {
                "model": "openai/gpt-4.1-mini",
                "auth": "secrets.model",
                "fallback": {"model": "openai/gpt-4.1-mini", "auth": "secrets.model"},
            }
        },
    )
    assert compiled["reasoning"]["reviewer"]["credential"] == "vault:models/review"
    assert compiled["reasoning"]["reviewer"]["fallback"]["key"] == "model"
    with pytest.raises(ConfigError, match="both auth and key"):
        compile_doc(
            tmp_path,
            {
                "reviewer": {
                    "model": "openai/gpt-4.1-mini",
                    "auth": "secrets.model",
                    "key": "secrets.model",
                }
            },
        )


@pytest.mark.parametrize("second", ["secrets.other", None])
def test_same_provider_cannot_mix_identities(tmp_path, second):
    using = {"agent": "claude"}
    if second:
        using["auth"] = second
    with pytest.raises(ConfigError, match="one credential"):
        compile_doc(
            tmp_path,
            {},
            [
                {
                    "first": {
                        "reason": "First",
                        "using": {"agent": "claude", "auth": "secrets.coder"},
                    }
                },
                {"second": {"reason": "Second", "using": using}},
            ],
        )


def test_agent_auth_does_not_cross_providers(tmp_path):
    compiled = compile_doc(
        tmp_path,
        {"default": {"agent": "codex", "auth": "secrets.coder"}},
        [
            {"first": {"reason": "First"}},
            {"second": {"reason": "Second", "using": {"agent": "claude"}}},
        ],
    )
    assert "credential" not in compiled["instructions"]["steps"]["second"]["policy"]


def test_local_named_agent_credential(monkeypatch, tmp_path):
    from outcomeci.vault import local

    monkeypatch.setattr(
        local,
        "resolve",
        lambda root, ref: {
            "credential_type": "agent",
            "configuration": {"provider": "codex"},
            "secrets": {
                "auth_json": json.dumps(
                    {"auth_mode": "chatgpt", "tokens": {"access_token": "test"}}
                )
            },
        },
    )
    login = launcher._local_login(tmp_path, "codex", "vault:agents/team")
    assert login["credential"] == {"auth_mode": "chatgpt", "tokens": {"access_token": "test"}}
    assert login["vault_ref"] == "vault:agents/team"
    with pytest.raises(ExecutionError, match="must hold an agent credential"):
        launcher._local_login(tmp_path, "claude", "vault:agents/team")


def test_local_rotation_updates_named_vault_not_machine_login(monkeypatch, tmp_path):
    from outcomeci.vault import local

    login = {"provider": "codex", "credential": {}, "vault_ref": "vault:agents/team"}
    monkeypatch.setattr(
        launcher, "_codex_rotation", lambda home, entry: {"tokens": {"refresh_token": "rotated"}}
    )
    writes = []
    monkeypatch.setattr(local, "rotate", lambda *args: writes.append(args))
    launcher._write_back_codex(launcher._AgentHold([login], tmp_path), tmp_path)
    assert writes[0][:2] == (tmp_path, "vault:agents/team")
    assert json.loads(writes[0][2]["auth_json"])["tokens"]["refresh_token"] == "rotated"


def test_debug_lease_selects_named_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        launcher,
        "issue_debug_lease",
        lambda *args, **kwargs: calls.append(kwargs) or {"agent": {"provider": "codex"}},
    )
    launcher._issue_leases("workspace", "workflow", ["codex"], {"codex": "vault:agents/team"})
    assert calls[0]["agent_credential"] == "vault:agents/team"


def test_training_labeler_auth_alias(tmp_path):
    path = tmp_path / "outcome.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v1",
                "trigger": "manual",
                "secrets": {"judge": "vault:models/judge"},
                "training": {"labeler": {"model": "openai/gpt-4.1-mini", "auth": "secrets.judge"}},
                "steps": [{"work": {"reason": "Do work"}}],
            }
        )
    )
    assert compile_workflow(path)["training"]["labeler"]["credential"] == "vault:models/judge"


def test_cli_builds_typed_agent_credentials():
    import argparse

    from outcomeci.vault.credentials import add_arguments, build

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(["--credential-type", "agent", "--agent-provider", "codex"])
    credential = build(
        "agent",
        args,
        value=json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "test"}}),
        secrets=None,
    )
    assert credential["configuration"] == {"provider": "codex"}
    assert set(credential["secrets"]) == {"auth_json"}
    with pytest.raises(ExecutionError, match="credential object"):
        build("agent", args, value="{}", secrets=None)


def test_runtime_only_steps_do_not_request_agent_identity():
    compiled = {
        "workflow": {"spec": {"agents": {"default": {"runner": "codex"}}}},
        "instructions": {
            "steps": {
                "wait": {"v1": {"kind": "await"}, "policy": {"runner": "codex"}},
                "choose": {"v1": {"kind": "decision"}, "policy": {"runner": "codex"}},
                "work": {
                    "v1": {"kind": "agent"},
                    "policy": {"runner": "claude", "credential": "vault:agents/team"},
                },
            }
        },
    }
    assert launcher._runners(compiled, None) == ["claude"]
    assert launcher._agent_credentials(compiled) == {"claude": "vault:agents/team"}
