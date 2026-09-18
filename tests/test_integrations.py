from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from outcomeci.config import ConfigError, compile_workflow
from outcomeci.integrations import (
    IntegrationError,
    IntegrationExecutor,
    apply_patch,
    doctor,
    import_openapi,
    propose_patch,
)
from outcomeci.process import ExecutionError


def workflow(tmp_path: Path) -> Path:
    instructions = tmp_path / ".outcomeci" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "standup.md").write_text("# Standup\n", encoding="utf-8")
    (instructions / "intake.md").write_text("# Intake\n", encoding="utf-8")
    value = {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "delivery"},
        "spec": {
            "triggers": {"manual": {"type": "manual"}},
            "backend": {"provider": "filesystem"},
            "context": {"provider": "filesystem", "include": []},
            "instructions": {"standup": {"path": ".outcomeci/instructions/standup.md"}},
            "agents": {
                "default": {"runner": "codex"},
                "phases": {
                    "intake": {
                        "instructions": ".outcomeci/instructions/intake.md",
                        "needs": [],
                        "integrations": [
                            {"type": "api", "capability": "tickets.create"},
                            {
                                "type": "human",
                                "timing": "after",
                                "id": "confirm_scope",
                                "participant": "requester",
                                "purpose": "Confirm the proposed scope.",
                                "interaction": "approval",
                            },
                        ],
                    }
                },
            },
            "connections": {
                "tickets": {
                    "provider": "http",
                    "base_url": "https://api.example.test",
                    "allow_private_network": True,
                    "auth": {
                        "type": "api_key",
                        "credential": "env:TICKET_TOKEN",
                        "header": "X-API-Key",
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
    compiled = compile_workflow(workflow(tmp_path))
    assert compiled["instructions"]["phases"]["intake"]["capabilities"] == ["tickets.create"]
    assert compiled["instructions"]["phases"]["intake"]["humans"]["after"][0]["id"] == (
        "confirm_scope"
    )
    assert "secret" not in json.dumps(compiled).lower()


def test_operation_policy_is_discoverable_and_audited(tmp_path: Path) -> None:
    compiled = compile_workflow(workflow(tmp_path))
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


def test_dry_run_includes_api_and_humans_without_credentials_or_io(tmp_path: Path) -> None:
    result = IntegrationExecutor(compile_workflow(workflow(tmp_path))).dry_run("intake")
    assert [item["name"] for item in result["api"]] == ["tickets.create"]
    assert result["humans"][0]["id"] == "confirm_scope"
    assert result["credentials_resolved"] is False
    assert result["requests_executed"] is False
    assert "TICKET_TOKEN" not in json.dumps(result)


def test_doctor_reports_credential_presence_without_value(tmp_path: Path, monkeypatch) -> None:
    compiled = compile_workflow(workflow(tmp_path))
    assert doctor(compiled)["summary"] == {"passed": 0, "failed": 1}
    monkeypatch.setenv("TICKET_TOKEN", "top-secret")
    result = doctor(compiled)
    assert result["summary"] == {"passed": 1, "failed": 0}
    assert result["credentials_exposed"] is False
    assert "top-secret" not in json.dumps(result)


def test_doctor_passes_basic_auth_type_with_preencoded_value_credential(
    tmp_path: Path,
) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "basic",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    result = doctor(compile_workflow(path), resolver=lambda _reference: {"value": "cGFpcg=="})

    shape_check = next(check for check in result["checks"] if check["check"] == "credential_shape")
    assert shape_check["status"] == "pass"
    assert "cGFpcg==" not in json.dumps(result)


def test_doctor_flags_basic_auth_type_paired_with_shapeless_credential(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "basic",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    result = doctor(compile_workflow(path), resolver=lambda _reference: {"client_id": "abc"})

    shape_check = next(check for check in result["checks"] if check["check"] == "credential_shape")
    assert shape_check["status"] == "fail"
    assert "api_key" in shape_check["detail"]


def test_doctor_passes_basic_auth_type_with_matching_username_password_credential(
    tmp_path: Path,
) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "basic",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    result = doctor(
        compile_workflow(path),
        resolver=lambda _reference: {"username": "u", "password": "p"},
    )

    shape_check = next(check for check in result["checks"] if check["check"] == "credential_shape")
    assert shape_check["status"] == "pass"


def test_doctor_passes_bearer_auth_type_with_matching_credential(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "bearer",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    result = doctor(compile_workflow(path), resolver=lambda _reference: {"value": "token"})

    shape_check = next(check for check in result["checks"] if check["check"] == "credential_shape")
    assert shape_check["status"] == "pass"


def test_http_failure_has_stable_safe_taxonomy(tmp_path: Path) -> None:
    executor = IntegrationExecutor(
        compile_workflow(workflow(tmp_path)),
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


def test_versioned_local_integration_package_is_merged_and_pinned(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    package_definition = {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeIntegrationPackage",
        "metadata": {"name": "tickets", "version": "1.2.0"},
        "spec": {
            "connections": value["spec"].pop("connections"),
            "integrations": value["spec"].pop("integrations"),
        },
    }
    package = tmp_path / ".outcomeci/integrations/tickets.yml"
    package.parent.mkdir(parents=True)
    package.write_text(yaml.safe_dump(package_definition, sort_keys=False))
    value["spec"]["integration_packages"] = [{"path": ".outcomeci/integrations/tickets.yml"}]
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    compiled = compile_workflow(path)
    assert "tickets.create" in IntegrationExecutor(compiled).capabilities("intake")
    assert compiled["workflow"]["spec"]["integration_packages"][0]["version"] == "1.2.0"


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
        compile_workflow(workflow(tmp_path)),
        resolver=lambda _reference: "top-secret",
        transport=httpx.MockTransport(handler),
    )
    result = executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")
    assert seen == {"credential": "top-secret", "body": '{"title":"Broken button"}'}
    assert result["output"] == {"id": "T-1", "url": "https://example.test/T-1"}
    assert "top-secret" not in json.dumps(result)
    assert "hidden" not in json.dumps(result)


def test_bearer_auth_type_honors_credential_scheme_override(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "bearer",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"value": "discord-bot-token", "scheme": "Bot"},
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    assert seen["authorization"] == "Bot discord-bot-token"


def test_basic_auth_type_sends_preencoded_value_credential_unmodified(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "basic",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"value": "ZW1haWwvdG9rZW46c2VjcmV0"},
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    assert seen["authorization"] == "Basic ZW1haWwvdG9rZW46c2VjcmV0"


def test_basic_auth_type_still_encodes_username_password_credential(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "basic",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"username": "user", "password": "pass"},
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    assert seen["authorization"] == f"Basic {base64.b64encode(b'user:pass').decode()}"


def test_rejects_undeclared_capability_and_invalid_input(tmp_path: Path) -> None:
    executor = IntegrationExecutor(
        compile_workflow(workflow(tmp_path)),
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
        compile_workflow(path)


def test_oauth2_token_exchange_defaults_to_client_credentials_grant(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "oauth2",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://auth.example.test/token",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "minted-token"})
        seen["bearer"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"client_id": "abc", "client_secret": "shh"},
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    assert parse_qs(seen["body"]) == {"grant_type": ["client_credentials"]}
    assert seen["bearer"] == "Bearer minted-token"


def test_oauth2_token_exchange_passes_configured_grant_type_and_account_id(
    tmp_path: Path,
) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "oauth2",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://zoom.us/oauth/token",
        "grant_type": "account_credentials",
        "account_id": "acct-123",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            seen["body"] = request.content.decode()
            seen["basic_auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"access_token": "minted-token"})
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"client_id": "abc", "client_secret": "shh"},
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    assert parse_qs(seen["body"]) == {
        "grant_type": ["account_credentials"],
        "account_id": ["acct-123"],
    }
    assert seen["basic_auth"].startswith("Basic ")


def test_oauth2_rejects_unsupported_grant_type(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "oauth2",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://auth.example.test/token",
        "grant_type": "password",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="grant_type is unsupported"):
        compile_workflow(path)


def test_oidc_rejects_unsupported_grant_type(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "oidc",
        "credential": "env:TICKET_TOKEN",
        "discovery_url": "https://auth.example.test/.well-known/openid-configuration",
        "grant_type": "password",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="grant_type is unsupported"):
        compile_workflow(path)


def test_oauth2_account_credentials_requires_account_id(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "oauth2",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://zoom.us/oauth/token",
        "grant_type": "account_credentials",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="account_id is required"):
        compile_workflow(path)


def _decode_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def test_jwt_bearer_signs_assertion_and_exchanges_for_bearer_token(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "jwt_bearer",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://oauth2.googleapis.com/token",
        "audience": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/analytics.readonly",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "minted-token"})
        seen["bearer"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {
            "issuer": "sa@project.iam.gserviceaccount.com",
            "private_key": pem,
        },
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    parsed = parse_qs(seen["body"])
    assert parsed["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    header_b64, payload_b64, signature_b64 = parsed["assertion"][0].split(".")
    header = json.loads(_decode_segment(header_b64))
    payload = json.loads(_decode_segment(payload_b64))
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload["iss"] == "sa@project.iam.gserviceaccount.com"
    assert payload["aud"] == "https://oauth2.googleapis.com/token"
    assert payload["scope"] == "https://www.googleapis.com/auth/analytics.readonly"
    assert "sub" not in payload
    private_key.public_key().verify(
        _decode_segment(signature_b64),
        f"{header_b64}.{payload_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert seen["bearer"] == "Bearer minted-token"


def test_jwt_bearer_includes_subject_for_domain_wide_delegation(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "jwt_bearer",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://login.salesforce.com/services/oauth2/token",
        "scope": "api",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "minted-token"})
        return httpx.Response(201, json={"id": "T-1", "url": "https://example.test/T-1"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {
            "issuer": "consumer-key",
            "subject": "integration@example.com",
            "private_key": pem,
        },
        transport=httpx.MockTransport(handler),
    )
    executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")

    parsed = parse_qs(seen["body"])
    _, payload_b64, _ = parsed["assertion"][0].split(".")
    payload = json.loads(_decode_segment(payload_b64))
    assert payload["sub"] == "integration@example.com"
    assert payload["aud"] == "https://login.salesforce.com/services/oauth2/token"


def test_jwt_bearer_rejects_unsupported_algorithm(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "jwt_bearer",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://oauth2.googleapis.com/token",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"issuer": "sa@example.com", "algorithm": "HS256"},
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
    )
    with pytest.raises(ExecutionError, match="algorithm"):
        executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")


def test_jwt_bearer_rejects_non_rsa_key(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "jwt_bearer",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://oauth2.googleapis.com/token",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {"issuer": "sa@example.com", "private_key": pem},
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
    )
    with pytest.raises(ExecutionError, match="RSA"):
        executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")


def test_jwt_bearer_rejects_malformed_private_key(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "jwt_bearer",
        "credential": "env:TICKET_TOKEN",
        "token_url": "https://oauth2.googleapis.com/token",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: {
            "issuer": "sa@example.com",
            "private_key": "not-a-pem-key",
        },
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})),
    )
    with pytest.raises(ExecutionError, match="PEM private key"):
        executor.execute("tickets.create", {"title": "Broken button"}, phase="intake")


def test_jwt_bearer_requires_token_url(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["connections"]["tickets"]["auth"] = {
        "type": "jwt_bearer",
        "credential": "env:TICKET_TOKEN",
    }
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="token_url is required"):
        compile_workflow(path)


def test_patch_has_lineage_and_requires_current_parent(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    definition = {
        "input": {"type": "object", "additionalProperties": False},
        "request": {"method": "GET", "path": "/v1/tickets"},
        "response": {"expose": {"items": "body.items"}},
    }
    patch_value = propose_patch(
        path,
        "tickets",
        "list",
        definition,
        reason="Pin a successful discovery",
        run="run-1",
        phase="intake",
        agent="codex",
    )
    patch_path = tmp_path / "patch.yml"
    patch_path.write_text(yaml.safe_dump(patch_value, sort_keys=False), encoding="utf-8")
    child = tmp_path / "outcome.next.yml"
    result = apply_patch(path, patch_path, child)
    assert result["parent_revision"] != result["workflow_revision"]
    assert result["provenance"] == {"run": "run-1", "phase": "intake", "agent": "codex"}
    assert (
        "list" in yaml.safe_load(child.read_text())["spec"]["integrations"]["tickets"]["operations"]
    )
    with pytest.raises(ConfigError, match="stale"):
        apply_patch(child, patch_path, tmp_path / "outcome.stale.yml")


def test_full_access_stays_inside_origin_and_method_policy(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["integrations"]["tickets"]["access"] = {
        "mode": "full",
        "methods": ["GET"],
        "expose": {"items": "body.items"},
    }
    value["spec"]["agents"]["phases"]["intake"]["capabilities"] = ["tickets.request"]
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": [1, 2], "secret": "hidden"})

    executor = IntegrationExecutor(
        compile_workflow(path),
        resolver=lambda _reference: "top-secret",
        transport=httpx.MockTransport(handler),
    )
    result = executor.execute(
        "tickets.request",
        {"method": "GET", "path": "/v1/items", "query": {"limit": 2}},
        phase="intake",
    )
    assert seen["url"] == "https://api.example.test/v1/items?limit=2"
    assert result["output"] == {"items": [1, 2]}
    with pytest.raises(ExecutionError, match="integration input is invalid"):
        executor.execute(
            "tickets.request", {"method": "DELETE", "path": "/v1/items"}, phase="intake"
        )


def test_imports_allowlisted_openapi_operations_as_patch(tmp_path: Path) -> None:
    path = workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["integrations"]["tickets"]["access"] = {
        "mode": "openapi",
        "source": "https://spec.example.test/openapi.json",
        "operations": ["createTicket"],
    }
    value["spec"]["integrations"]["tickets"]["operations"] = {}
    value["spec"]["agents"]["phases"]["intake"]["integrations"] = [
        {"type": "api", "capability": "tickets.createticket"}
    ]
    value["spec"]["agents"]["phases"]["intake"]["capabilities"] = ["tickets.createticket"]
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    document = {
        "openapi": "3.1.0",
        "paths": {
            "/v1/tickets": {
                "post": {
                    "operationId": "createTicket",
                    "summary": "Create a ticket",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["title"],
                                    "properties": {"title": {"type": "string"}},
                                }
                            }
                        },
                    },
                }
            }
        },
    }
    patch = import_openapi(
        path,
        "tickets",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=document)),
    )
    assert list(patch["spec"]["operations"]["add"]) == ["tickets.createticket"]
    assert patch["metadata"]["parentRevision"] == compile_workflow(path)["workflow_revision"]
