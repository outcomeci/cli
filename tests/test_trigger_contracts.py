from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from lowered import email_payload

from outcomeci.cli import main
from outcomeci.config import ConfigError
from outcomeci.contracts import (
    ContractError,
    contract_schema,
    render_reference,
    validate_contract,
)


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
