from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from lowered import compile_file

from outcomeci.config import ConfigError
from outcomeci.integrations import (
    IntegrationError,
    IntegrationExecutor,
    doctor,
)
from outcomeci.process import ExecutionError


def workflow(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    value = {
        "apiVersion": "outcomeci.workflow/v1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "delivery"},
        "spec": {
            "triggers": {"manual": {"type": "manual"}},
            "instructions": {"workflow": {"content": "Deliver the outcome."}},
            "agents": {
                "default": {"runner": "codex"},
                "phases": {
                    "intake": {
                        "instructions": {"content": "Create the ticket."},
                        "needs": [],
                        "capabilities": ["tickets.create"],
                    }
                },
            },
            "connections": {
                "tickets": {
                    "provider": "http",
                    "base_url": "https://api.example.test",
                    "allow_private_network": True,
                    "auth": {
                        "connector": "tickets",
                        "credential": "env:TICKET_TOKEN",
                        "accepts": [
                            {"kind": "api_key", "header": "X-API-Key", "credential": ["api_key"]}
                        ],
                    },
                }
            },
            "integrations": {
                "tickets": {
                    "connection": "tickets",
                    "access": {"mode": "schema"},
                    "operations": {
                        "create": {
                            "description": "Create a ticket",
                            "input": {
                                "type": "object",
                                "required": ["title"],
                                "properties": {"title": {"type": "string"}},
                                "additionalProperties": False,
                            },
                            "request": {
                                "method": "POST",
                                "path": "/v1/tickets",
                                "body": {"title": "{{ input.title }}"},
                            },
                            "response": {
                                "expose": {
                                    "id": "body.id",
                                    "url": "body.url",
                                }
                            },
                        }
                    },
                }
            },
        },
    }
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def test_compiles_phase_scoped_capability(tmp_path: Path) -> None:
    compiled = compile_file(workflow(tmp_path))
    assert compiled["instructions"]["phases"]["intake"]["capabilities"] == ["tickets.create"]
    assert "secret" not in json.dumps(compiled).lower()


def test_operation_policy_is_discoverable_and_audited(tmp_path: Path) -> None:
    compiled = compile_file(workflow(tmp_path))
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda _reference: "secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(201, json={"id": "1"})),
    )
    assert executor.describe("tickets.create")["policy"] == {
        "side_effect": "execute",
        "approval": "inherit",
        "idempotency": "none",
    }
    result = executor.execute("tickets.create", {"title": "Test"}, phase="intake")
    assert result["audit"]["policy"] == executor.describe("tickets.create")["policy"]


def test_dry_run_describes_api_calls_without_credentials_or_io(tmp_path: Path) -> None:
    result = IntegrationExecutor(compile_file(workflow(tmp_path))).dry_run("intake")
    assert [item["name"] for item in result["api"]] == ["tickets.create"]
    assert result["credentials_resolved"] is False
    assert result["requests_executed"] is False
    assert "TICKET_TOKEN" not in json.dumps(result)


def test_doctor_reports_credential_presence_without_value(tmp_path: Path, monkeypatch) -> None:
    compiled = compile_file(workflow(tmp_path))
    assert doctor(compiled)["summary"] == {"passed": 0, "failed": 1}
    monkeypatch.setenv("TICKET_TOKEN", "top-secret")
    result = doctor(compiled)
    assert result["summary"] == {"passed": 1, "failed": 0}
    assert result["credentials_exposed"] is False
    assert "top-secret" not in json.dumps(result)


def test_http_failure_has_stable_safe_taxonomy(tmp_path: Path) -> None:
    executor = IntegrationExecutor(
        compile_file(workflow(tmp_path)),
        resolver=lambda _reference: "secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(403, text="private")),
    )
    with pytest.raises(IntegrationError) as raised:
        executor.execute("tickets.create", {"title": "Test"}, phase="intake")
    assert raised.value.as_dict() == {
        "code": "integration.authorization_failed",
        "category": "authorization",
        "message": "integration request returned HTTP 403",
        "retryable": False,
    }
    assert "private" not in str(raised.value)


def test_executes_with_credential_but_returns_only_projected_output(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["credential"] = request.headers["X-API-Key"]
        seen["body"] = request.content.decode()
        return httpx.Response(
            201,
            json={"id": "T-1", "url": "https://example.test/T-1", "private": "hidden"},
        )

    executor = IntegrationExecutor(
        compile_file(workflow(tmp_path)),
        resolver=lambda _reference: "top-secret",
        transport=httpx.MockTransport(handler),
    )
    result = executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")
    assert seen == {"credential": "top-secret", "body": '{"title":"Broken button"}'}
    assert result["output"] == {"id": "T-1", "url": "https://example.test/T-1"}
    assert "top-secret" not in json.dumps(result)
    assert "hidden" not in json.dumps(result)


def test_execute_emits_diagnostic_markers_bracketing_the_network_call(
    tmp_path: Path, capsys
) -> None:
    """These are the only durable record of an in-flight call if the process
    is killed abruptly before the normal completed/failed event can be
    written (observed once: a Slack post whose response never got read)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_file(workflow(tmp_path)),
        resolver=lambda _reference: "top-secret",
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    captured = capsys.readouterr().err
    lines = [json.loads(line) for line in captured.strip().splitlines()]
    sending = next(line for line in lines if line["event"] == "integration_request_sending")
    received = next(line for line in lines if line["event"] == "integration_request_received")
    assert sending["capability"] == "tickets.create"
    assert sending["phase"] == "intake"
    assert received["status"] == 201
    assert isinstance(received["elapsed_ms"], int)
    assert "top-secret" not in captured


def test_rejects_undeclared_capability_and_invalid_input(tmp_path: Path) -> None:
    executor = IntegrationExecutor(
        compile_file(workflow(tmp_path)),
        resolver=lambda _reference: "secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    with pytest.raises(ExecutionError, match="not authorized"):
        executor.execute("tickets.delete", {}, phase="intake")
    with pytest.raises(ExecutionError, match="required property"):
        executor.execute("tickets.create", {}, phase="intake")


def test_rejects_absolute_operation_path(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["integrations"]["tickets"]["operations"]["create"]["request"]["path"] = (
        "https://evil.example/steal"
    )
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ConfigError, match="relative absolute-path"):
        compile_file(path)
