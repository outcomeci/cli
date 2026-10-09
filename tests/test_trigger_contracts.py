from __future__ import annotations

import copy
from pathlib import Path

import pytest
from lowered import email_payload

from outcomeci.workflow.compiler import ConfigError
from outcomeci.workflow.contracts import ContractError, contract_schema, validate_contract


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
    from outcomeci.workflow.compiler import _relative_path

    private = tmp_path / ".outcomeci" / "vault.enc"
    private.parent.mkdir()
    private.write_text("synthetic ciphertext")
    (tmp_path / "instructions.md").symlink_to(private)
    with pytest.raises(ConfigError, match="broker-private"):
        _relative_path(tmp_path, "instructions.md", "instructions")
