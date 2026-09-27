from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from outcomeci import capability
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize


def _broker_without_socket(tmp_path: Path, monkeypatch):
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
    value = yaml.safe_load(workflow.read_text())
    hook = value["spec"]["agents"]["phases"]["intake"]["integrations"][0]
    hook["delivery"] = {
        "type": "custom",
        "connection": "people_api",
        "targets": [{"kind": "user", "name": "isaah"}],
    }
    value["spec"]["connections"] = [
        {
            "ref": "people_api",
            "provider": "custom",
            "transport": {"type": "http", "endpoint": "https://people.example.test"},
            "operations": {
                "request": {"method": "POST", "path": "/requests"},
                "poll": {"method": "GET", "path": "/requests/{correlation_id}"},
            },
            "contract": {
                "request": {
                    "input": {"type": "object", "required": ["run_id", "targets"]},
                    "output": {"type": "object", "required": ["correlation_id"]},
                },
                "poll": {"output": {"type": "object", "required": ["responses"]}},
            },
        }
    ]
    workflow.write_text(yaml.safe_dump(value, sort_keys=False))
    monkeypatch.setattr(capability, "_Server", lambda *args: object())
    broker = capability.Broker.__new__(capability.Broker)
    compiled = capability.compile_workflow(workflow)
    broker.hooks = {
        "confirm_intent": compiled["instructions"]["phases"]["intake"]["humans"]["after"][0]
    }
    broker.root, broker.config, broker.run_id = tmp_path, workflow, "run-1"
    broker.token = "secret"
    return broker


def test_broker_rejects_wrong_run_and_undeclared_hook(tmp_path: Path, monkeypatch) -> None:
    broker = _broker_without_socket(tmp_path, monkeypatch)
    with pytest.raises(ExecutionError, match="not authorized"):
        broker.dispatch(
            {
                "token": "secret",
                "operation": "poll",
                "run_id": "other",
                "interaction_id": "confirm_intent",
            }
        )
    with pytest.raises(ExecutionError, match="not authorized"):
        broker.dispatch(
            {"token": "secret", "operation": "poll", "run_id": "run-1", "interaction_id": "other"}
        )


def test_broker_accepts_only_transport_verified_response(tmp_path: Path, monkeypatch) -> None:
    broker = _broker_without_socket(tmp_path, monkeypatch)
    monkeypatch.setattr(
        capability, "transport_responses", lambda *args: [{"from": "isaah", "message": "approved"}]
    )
    interaction = tmp_path / ".outcomeci/outcomes/run-1/interactions/intake/confirm_intent.json"
    interaction.parent.mkdir(parents=True)
    interaction.write_text(
        '{"run_id":"run-1","phase":"intake","id":"confirm_intent","delivery":{"type":"custom"}}'
    )
    monkeypatch.setattr(capability, "accept", lambda *args: {"status": "running"})
    with pytest.raises(ExecutionError, match="not verified"):
        broker.dispatch(
            {
                "token": "secret",
                "operation": "accept",
                "run_id": "run-1",
                "interaction_id": "confirm_intent",
                "message": "fabricated",
            }
        )
    result = broker.dispatch(
        {
            "token": "secret",
            "operation": "accept",
            "run_id": "run-1",
            "interaction_id": "confirm_intent",
            "message": "approved",
            "approve": True,
        }
    )
    assert result["status"] == "running"
