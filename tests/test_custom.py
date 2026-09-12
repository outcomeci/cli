from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from outcomeci import custom, humans
from outcomeci.config import compile_workflow
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize


def _configure(tmp_path: Path) -> Path:
    initialize(tmp_path, "filesystem")
    path = tmp_path / "outcome.yml"
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"] = [{
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
    }]
    hook = value["spec"]["agents"]["phases"]["intake"]["humans"]["after"][0]
    hook["delivery"] = {"type": "custom", "connection": "people_api", "targets": [{"kind": "user", "name": "izzy"}]}
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return path


class _Response:
    headers = {"Content-Type": "application/json"}

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.value).encode()


def test_http_custom_transport_validates_contract_and_uses_external_auth(tmp_path: Path, monkeypatch) -> None:
    config = _configure(tmp_path)
    value = yaml.safe_load(config.read_text())
    value["spec"]["connections"][0]["auth"] = {"env": "PEOPLE_TOKEN"}
    config.write_text(yaml.safe_dump(value, sort_keys=False))
    monkeypatch.setenv("PEOPLE_TOKEN", "secret")
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return _Response({"correlation_id": "human-1"})

    monkeypatch.setattr(custom.urllib.request, "urlopen", open_request)
    result = custom.call(config, "request", {"schema_version": "outcomeci.human-request/v1alpha1", "run_id": "run-1", "phase": "intake", "interaction_id": "review", "interaction": "review", "purpose": "Review.", "targets": [{"kind": "user", "name": "izzy"}]}, "people_api")
    assert result == {"correlation_id": "human-1"}
    assert requests[0].headers["Authorization"] == "Bearer secret"
    assert "secret" not in config.read_text()


def test_custom_contract_rejects_invalid_request_before_transport(tmp_path: Path) -> None:
    config = _configure(tmp_path)
    with pytest.raises(ExecutionError, match="violates its contract"):
        custom.call(config, "request", {"targets": []}, "people_api")


def test_custom_human_connection_compiles(tmp_path: Path) -> None:
    config = _configure(tmp_path)
    compiled = compile_workflow(config)
    hook = compiled["instructions"]["phases"]["intake"]["humans"]["after"][0]
    assert hook["delivery"]["type"] == "custom"


def test_human_request_and_poll_use_custom_adapter(tmp_path: Path, monkeypatch) -> None:
    config = _configure(tmp_path)
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    interaction = outcome / "interactions/intake/confirm_intent.json"
    interaction.parent.mkdir(parents=True)
    interaction.write_text(json.dumps({
        "run_id": "run-1", "phase": "intake", "id": "confirm_intent", "status": "pending",
        "interaction": "approval", "purpose": "Confirm scope.",
        "delivery": {"type": "custom", "connection": "people_api", "targets": [{"kind": "user", "name": "izzy"}]},
        "wait": {"strategy": "ask"},
    }))
    (outcome / "run.json").write_text(json.dumps({"run_id": "run-1", "status": "awaiting_input", "pending_interaction": {"id": "confirm_intent", "path": str(interaction)}}))
    calls = []

    def adapter(_config, operation, payload, connection):
        calls.append((operation, payload, connection))
        if operation == "request":
            return {"correlation_id": "human-1"}
        return {"responses": [{"from": "izzy", "message": "Proceed", "responded_at": "2026-09-12T00:00:00Z"}]}

    monkeypatch.setattr(humans, "call_custom", adapter)
    delivered = humans.request(tmp_path, config, "run-1", "confirm_intent")
    assert delivered["correlation_id"] == "human-1"
    polled = humans.poll(tmp_path, config, "run-1", "confirm_intent")
    assert polled["responses"][0]["from"] == "izzy"
    assert [item[0] for item in calls] == ["request", "poll"]
