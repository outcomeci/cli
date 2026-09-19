from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path

import httpx
import pytest
import yaml

from outcomeci import local
from outcomeci.cli import main
from outcomeci.config import ConfigError, compile_workflow
from outcomeci.contracts import (
    ContractError,
    contract_schema,
    render_reference,
    validate_contract,
)
from outcomeci.integrations import IntegrationError, IntegrationExecutor
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize
from outcomeci.schema import load_schema


def typed_workflow(root: Path) -> Path:
    initialize(root, "filesystem")
    path = root / "outcome.yml"
    value = yaml.safe_load(path.read_text())
    del value["spec"]["instructions"]
    value["spec"]["triggers"] = {"inbound": {"type": "email.received"}}
    value["spec"]["agents"] = {
        "default": {"runner": "codex", "model": "default-model"},
        "orchestrator": {"instructions": ".outcomeci/instructions/standup.md"},
        "phases": {
            "notify": {
                "type": "agent",
                "instructions": ".outcomeci/instructions/plan.md",
                "needs": [],
                "with": {"recipient": {"type": "user", "value": "@izzy"}},
                "integrations": [{"type": "api", "capability": "slack.request", "required": True}],
                "expects": {
                    "inputs": [
                        {
                            "name": "email",
                            "from": "trigger.inbound",
                            "media_type": "application/json",
                        }
                    ],
                    "outputs": [
                        {
                            "name": "delivery",
                            "path": "delivery.json",
                            "media_type": "application/json",
                            "schema": {
                                "type": "object",
                                "required": ["status"],
                                "properties": {
                                    "status": {"enum": ["delivered", "failed", "uncertain"]}
                                },
                            },
                        }
                    ],
                },
            }
        },
    }
    (root / "policy.md").write_text("Review only authorized email notification requests.\n")
    value["spec"]["connections"] = {
        "slack": {
            "provider": "http",
            "base_url": "https://slack.com",
            "auth": {"type": "bearer", "credential": "vault:slack/bot-token"},
        }
    }
    value["spec"]["integrations"] = {
        "slack": {
            "connection": "slack",
            "access": {
                "mode": "full",
                "methods": ["GET", "POST"],
                "max_requests": 8,
                "opaque_identifiers": True,
            },
            "policy": {"instructions": "policy.md"},
        }
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return path


def email_payload() -> dict:
    return copy.deepcopy(contract_schema("email.received")["examples"][0])


def test_typed_configuration_compiles_and_preserves_enforcement_contract(
    tmp_path: Path,
) -> None:
    path = typed_workflow(tmp_path)
    import jsonschema

    jsonschema.validate(yaml.safe_load(path.read_text()), load_schema())
    compiled = compile_workflow(path)
    instructions = compiled["instructions"]
    phase = instructions["phases"]["notify"]
    assert phase["type"] == "agent"
    assert phase["with"]["recipient"]["value"] == "@izzy"
    assert phase["required_capabilities"] == ["slack.request"]
    assert instructions["orchestrator"]["policy"] == {
        "runner": "codex",
        "model": "default-model",
    }
    assert instructions["integration_policies"]["slack"]["policy"] == {
        "runner": "codex",
        "model": "default-model",
    }
    schema_key = phase["expects"]["inputs"][0]["schema"]
    assert instructions["schemas"][schema_key]["value"] == contract_schema("email.received")
    assert compiled["workflow"]["spec"]["integrations"]["slack"]["access"]["max_requests"] == 8


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["spec"]["agents"]["phases"]["notify"].pop("type"),
        lambda value: value["spec"]["agents"]["phases"]["notify"].update(type="script"),
        lambda value: value["spec"]["agents"]["phases"]["notify"].update(typo="ignored"),
        lambda value: value["spec"]["agents"]["phases"]["notify"].update(with_value=[]),
        lambda value: value["spec"]["agents"]["phases"]["notify"]["integrations"][0].update(
            required="yes"
        ),
        lambda value: value["spec"].update(instructions={"standup": "policy.md"}),
        lambda value: value["spec"]["integrations"]["slack"]["access"].update(max_requests=True),
        lambda value: value["spec"]["integrations"]["slack"]["access"].update(max_requests=0),
        lambda value: value["spec"]["integrations"]["slack"]["access"].update(
            opaque_identifiers="yes"
        ),
        lambda value: value["spec"]["integrations"]["slack"]["policy"].update(
            instructions="../escape.md"
        ),
        lambda value: value["spec"]["agents"]["phases"]["notify"]["expects"]["outputs"][0].update(
            schema={"type": "not-a-type"}
        ),
    ],
)
def test_rejects_invalid_typed_configuration(tmp_path: Path, mutation) -> None:
    path = typed_workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    mutation(value)
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ConfigError):
        compile_workflow(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "v2"),
        ("type", "manual"),
        ("event_id", ""),
        ("sender", 42),
        ("sender", "not-an-email"),
        ("recipients", []),
        ("received_at", "yesterday"),
        ("text_body", []),
        ("artifacts", [{"token": "do-not-echo"}]),
    ],
)
def test_email_payload_is_typed_and_errors_do_not_echo_values(field: str, value) -> None:
    payload = email_payload()
    payload[field] = value
    with pytest.raises(ContractError) as error:
        validate_contract("email.received", payload)
    assert "do-not-echo" not in str(error.value)


def test_email_example_and_nullable_artifact_fields_validate() -> None:
    payload = email_payload()
    payload["artifacts"] = [
        {
            "artifact_ref": "attachment-1",
            "kind": "attachment",
            "content_type": "image/jpeg",
            "filename": None,
            "byte_size": 0,
        }
    ]
    validate_contract("email.received", payload)
    del payload["event_id"]
    with pytest.raises(ContractError):
        validate_contract("email.received", payload)


def test_cron_example_validates_and_rejects_unknown_fields() -> None:
    payload = copy.deepcopy(contract_schema("cron")["examples"][0])
    validate_contract("cron", payload)
    payload["unexpected"] = "value"
    with pytest.raises(ContractError):
        validate_contract("cron", payload)


def test_invalid_trigger_never_invokes_agent(tmp_path: Path, monkeypatch) -> None:
    path = typed_workflow(tmp_path)
    invoked = []
    monkeypatch.setattr(local, "_execute", lambda *args, **kwargs: invoked.append(True))
    with pytest.raises(ExecutionError, match="contract failed"):
        local.trigger(tmp_path, path, "inbound", {"subject": "Incomplete email"})
    assert invoked == []


def test_valid_trigger_materializes_exact_input_and_trusted_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    path = typed_workflow(tmp_path)
    payload = email_payload()
    monkeypatch.setattr(local, "_execute", lambda _root, _config, state, **kwargs: state)
    state = local.trigger(tmp_path, path, "inbound", payload)
    compiled = compile_workflow(path)
    inputs = local._input_context(compiled, tmp_path, "notify", state)
    assert inputs[0]["value"] == payload
    payload["subject"] = "Changed later"
    assert state["trigger"]["value"]["subject"] == "Receipt arrived"
    with pytest.raises(ExecutionError, match="unavailable"):
        local._input_context(compiled, tmp_path, "notify", {"intent": "Wrong input"})


def test_policy_instruction_edits_change_revision(tmp_path: Path) -> None:
    path = typed_workflow(tmp_path)
    first = compile_workflow(path)["workflow_revision"]
    (tmp_path / "policy.md").write_text("Deny everything.\n")
    assert compile_workflow(path)["workflow_revision"] != first


def test_policy_configuration_fails_closed_until_executor_is_wired(
    tmp_path: Path,
) -> None:
    calls = []
    executor = IntegrationExecutor(
        compile_workflow(typed_workflow(tmp_path)),
        resolver=lambda reference: calls.append(reference) or "secret",
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)),
    )
    with pytest.raises(IntegrationError, match="not wired yet"):
        executor.execute(
            "slack.request",
            {"method": "GET", "path": "/api/users.list"},
            phase="notify",
        )
    assert calls == []


def test_inline_output_contract_is_enforced(tmp_path: Path) -> None:
    compiled = compile_workflow(typed_workflow(tmp_path))
    (tmp_path / "delivery.json").write_text('{"status":"made-up"}')
    with pytest.raises(ExecutionError, match="validation"):
        local._validate_outputs(compiled, tmp_path, "notify")
    (tmp_path / "delivery.json").write_text('{"status":"delivered"}')
    local._validate_outputs(compiled, tmp_path, "notify")


def test_required_integration_needs_broker_confirmed_success(tmp_path: Path) -> None:
    compiled = compile_workflow(typed_workflow(tmp_path))
    with pytest.raises(ExecutionError, match="evidence is missing"):
        local._validate_required_effects(tmp_path, compiled, "run-1", "notify")
    broker = tmp_path / ".outcomeci/.broker/run-1"
    broker.mkdir(parents=True)
    journal = broker / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "calls": {
                    "receipt": {
                        "capability": "slack.request",
                        "status": "confirmed",
                        "result": {"ok": False},
                    }
                }
            }
        )
    )
    with pytest.raises(ExecutionError, match="slack.request"):
        local._validate_required_effects(tmp_path, compiled, "run-1", "notify")
    journal.write_text(
        json.dumps(
            {
                "calls": {
                    "receipt": {
                        "capability": "slack.request",
                        "status": "confirmed",
                        "result": {
                            "ok": True,
                            "output": {"result": {"ok": True}},
                        },
                    }
                }
            }
        )
    )
    local._validate_required_effects(tmp_path, compiled, "run-1", "notify")


def test_effect_receipts_are_sanitized_and_materialized_for_repair(tmp_path: Path) -> None:
    broker = tmp_path / ".outcomeci/.broker/run-1"
    broker.mkdir(parents=True)
    (broker / "journal.json").write_text(
        json.dumps(
            {
                "calls": {
                    "digest": {
                        "capability": "slack.request",
                        "proposal_sha256": "a" * 64,
                        "status": "confirmed",
                        "request": {"authorization": "Bearer secret", "body": "private"},
                        "result": {
                            "ok": True,
                            "status": 200,
                            "output": {"result": {"ok": True, "channel": "D123456789"}},
                        },
                    }
                }
            }
        )
    )
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    outcome.mkdir(parents=True)
    receipt = local._write_effect_receipts(tmp_path, outcome, "run-1", "notify")
    value = json.loads(receipt.read_text())
    assert value == {
        "schema_version": "1",
        "phase": "notify",
        "effects": [
            {
                "capability": "slack.request",
                "proposal_sha256": "a" * 64,
                "status": "confirmed",
                "ok": True,
                "http_status": 200,
            }
        ],
    }
    assert "secret" not in receipt.read_text()
    assert "D123456789" not in receipt.read_text()


def test_output_repair_has_no_capability_environment(tmp_path: Path, monkeypatch) -> None:
    compiled = compile_workflow(typed_workflow(tmp_path))
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    outcome.mkdir(parents=True)
    (outcome / "effects.json").write_text('{"schema_version":"1","effects":[]}')
    calls = []

    def invoke(*args, **kwargs):
        calls.append((args, kwargs))
        (outcome / "delivery.json").write_text('{"status":"delivered"}')
        return "repaired"

    monkeypatch.setattr(local, "invoke", invoke)
    summary = local._repair_outputs(
        tmp_path,
        compiled,
        outcome,
        "notify",
        "codex",
        None,
        ExecutionError("missing required output"),
        [outcome / "delivery.json"],
        {"SLACK_TOKEN"},
        container_isolated=True,
    )
    assert summary == "repaired"
    assert calls[0][1]["extra_env"] == {}
    assert calls[0][1]["excluded_env"] == {"SLACK_TOKEN"}
    assert calls[0][1]["container_isolated"] is True
    assert "must not be repeated" in calls[0][0][2]


def test_execution_repairs_invalid_output_once_without_capabilities(
    tmp_path: Path, monkeypatch
) -> None:
    path = typed_workflow(tmp_path)
    calls = []
    events = []
    monkeypatch.setattr(local, "serve_capability", lambda *args, **kwargs: nullcontext({}))
    monkeypatch.setattr(local, "_validate_required_effects", lambda *args: None)
    monkeypatch.setattr(
        local,
        "_transcripts",
        lambda *args, **kwargs: {"usage_records": 0, "files": [], "usage": []},
    )

    def invoke(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            output = next(item for item in kwargs["writable_paths"] if item.name == "delivery.json")
            output.write_text('{"status":"delivered"}')
        return "complete"

    monkeypatch.setattr(local, "invoke", invoke)
    state = local.trigger(tmp_path, path, "inbound", email_payload(), event_sink=events.append)

    assert state["status"] == "awaiting_confirmation"
    assert len(calls) == 2
    assert calls[1]["extra_env"] == {}
    assert [item["event_type"] for item in events] == [
        "artifact.repair_started",
        "artifact.repair_completed",
    ]


def test_instruction_symlink_cannot_read_private_vault(tmp_path: Path) -> None:
    from outcomeci.config import _relative_path

    private = tmp_path / ".outcomeci" / "vault.enc"
    private.parent.mkdir()
    private.write_text("synthetic ciphertext")
    (tmp_path / "instructions.md").symlink_to(private)
    with pytest.raises(ConfigError, match="broker-private"):
        _relative_path(tmp_path, "instructions.md", "instructions")


def test_contract_schema_cli_and_generated_documentation(tmp_path: Path, capsys) -> None:
    payload = tmp_path / "email.json"
    payload.write_text(json.dumps(email_payload()))
    assert main(["schema", "validate", str(payload), "--type", "email.received"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["schema", "print", "--type", "agent"]) == 0
    assert json.loads(capsys.readouterr().out)["required"] == ["type", "instructions"]
    cron_payload = tmp_path / "cron.json"
    cron_payload.write_text(json.dumps(contract_schema("cron")["examples"][0]))
    assert main(["schema", "validate", str(cron_payload), "--type", "cron"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["schema", "print", "--type", "cron"]) == 0
    assert json.loads(capsys.readouterr().out)["required"] == [
        "schema_version",
        "type",
        "schedule_id",
        "generation",
        "schedule_arn",
        "scheduled_at",
        "execution_id",
        "attempt_number",
        "trigger_name",
    ]
    assert main(["schema", "docs"]) == 0
    assert capsys.readouterr().out.rstrip() == render_reference().rstrip()
    reference = Path(__file__).parents[1] / "docs" / "typed-contracts-v1.md"
    assert reference.read_text().rstrip() == render_reference().rstrip()
    assert "`artifacts[].artifact_ref`" in render_reference()
