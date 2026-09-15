from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
import yaml
from test_typed_contracts import typed_workflow

from outcomeci import local
from outcomeci.config import ConfigError, compile_workflow
from outcomeci.contracts import ContractError, validate_contract
from outcomeci.process import ExecutionError
from outcomeci.webhooks import (
    Listener,
    ListenerUnavailable,
    validate_delivery_config,
)


def test_claim_transport_failure_is_retryable(tmp_path, monkeypatch):
    from outcomeci import cloud

    listener = Listener("ws", "wf", tmp_path, workflow(tmp_path))
    monkeypatch.setattr(cloud, "_authorized_request", lambda *args, **kwargs: (503, {}))
    with pytest.raises(ListenerUnavailable):
        listener.claim()


def test_claim_permission_failure_is_not_retryable(tmp_path, monkeypatch):
    from outcomeci import cloud

    listener = Listener("ws", "wf", tmp_path, workflow(tmp_path))
    monkeypatch.setattr(cloud, "_authorized_request", lambda *args, **kwargs: (403, {}))
    with pytest.raises(ExecutionError) as error:
        listener.claim()
    assert not isinstance(error.value, ListenerUnavailable)


def workflow(root: Path):
    path = typed_workflow(root)
    value = yaml.safe_load(path.read_text())
    value["spec"]["triggers"] = {"inbound": {"type": "webhook.received", "delivery": "queued"}}
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return path


def payload():
    return {
        "schema_version": "outcomeci.trigger.webhook.received/v1",
        "type": "webhook.received",
        "event_id": "proof-1",
        "received_at": "2026-09-15T12:00:00Z",
        "method": "POST",
        "query": "",
        "headers": {"content-type": "application/json"},
        "body_base64": "eyJtZXNzYWdlIjoiaGVsbG8ifQ==",
    }


def test_webhook_compilation_inherits_typed_input(tmp_path):
    compiled = compile_workflow(workflow(tmp_path))
    assert compiled["triggers"]["inbound"]["delivery"] == "queued"
    key = compiled["instructions"]["phases"]["notify"]["expects"]["inputs"][0]["schema"]
    assert (
        compiled["instructions"]["schemas"][key]["value"]["properties"]["type"]["const"]
        == "webhook.received"
    )


@pytest.mark.parametrize(
    "value",
    [
        {"delivery": "bad"},
        {"delivery": "forward"},
        {"delivery": "auto"},
        {"timeout_seconds": True},
        {"timeout_seconds": 26},
        {"filters": {}},
        {"response": {"status": 199}},
    ],
)
def test_invalid_delivery_configuration(value):
    with pytest.raises(ConfigError):
        validate_delivery_config({"type": "webhook.received", **value})


def test_typed_webhook_rejects_invalid_base64():
    value = payload()
    value["body_base64"] = "invalid!"
    with pytest.raises(ContractError):
        validate_contract("webhook.received", value)


def test_invalid_claim_is_failed_without_starting_agent(tmp_path, monkeypatch):
    listener = Listener("ws", "wf", tmp_path, workflow(tmp_path))
    calls = []
    monkeypatch.setattr(
        listener, "request", lambda operation, body: calls.append((operation, body)) or {}
    )
    value = payload()
    value["body_base64"] = "invalid!"
    with pytest.raises(ContractError):
        listener.execute(
            {
                "invocation_id": str(uuid4()),
                "lease_token": str(uuid4()),
                "trigger_type": "webhook.received",
                "trigger_name": "inbound",
                "input": value,
            }
        )
    assert len(calls) == 1 and calls[0][0] == "complete"
    assert calls[0][1]["status"] == "failed"


def test_changed_support_file_requires_resync(tmp_path):
    config = workflow(tmp_path)
    listener = Listener("ws", "wf", tmp_path, config)
    (tmp_path / ".outcomeci/instructions/standup.md").write_text("changed")
    with pytest.raises(ExecutionError, match="changed"):
        listener.claim()


def test_private_local_files_do_not_change_synced_digest(tmp_path):
    listener = Listener("ws", "wf", tmp_path, workflow(tmp_path))
    private = tmp_path / ".outcomeci/.broker"
    private.mkdir()
    (private / "secret.json").write_text("synthetic private map")
    (tmp_path / ".outcomeci/vault.enc").write_text("synthetic vault")
    assert listener._support_hash() == listener.support_sha256


def test_completed_local_delivery_cannot_be_replayed(tmp_path, monkeypatch):
    listener = Listener("ws", "wf", tmp_path, workflow(tmp_path))
    calls = []
    monkeypatch.setattr(
        listener, "request", lambda operation, body: calls.append((operation, body)) or {}
    )
    execute = Mock(
        return_value={"run_id": "proof", "completed_phases": ["notify"], "ready_phases": []}
    )
    monkeypatch.setattr(local, "trigger", execute)
    claim = {
        "invocation_id": str(uuid4()),
        "lease_token": str(uuid4()),
        "trigger_name": "inbound",
        "trigger_type": "webhook.received",
        "input": payload(),
    }
    assert listener.execute(claim)["run_id"] == "proof"
    assert [operation for operation, body in calls] == ["start", "complete"]
    with pytest.raises(ExecutionError, match="receipt"):
        listener.execute(claim)
    execute.assert_called_once()
