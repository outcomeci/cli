"""`uses: custom`: the workflow names the origin, a static credential signs."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from outcomeci.broker.executor import IntegrationError, IntegrationExecutor
from outcomeci.broker.policy import PolicyExecutor
from outcomeci.workflow.compiler import ConfigError, compile_workflow


def compiled(tmp_path, *, binding=None, can=None):
    path = tmp_path / "orders.outcome.yaml"
    doc = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "secrets": {"acme": "vault:acme/token"},
        "apis": {
            "acme": binding
            or {"uses": "custom", "base_url": "https://api.acme.test", "auth": "secrets.acme"}
        },
        "steps": [
            {
                "sync": {
                    "reason": "Reconcile orders.",
                    "can": can if can is not None else [{"acme.read": {"path": "/v1/orders"}}],
                }
            }
        ],
    }
    path.write_text(yaml.safe_dump(doc))
    return compile_workflow(path)


def broker(tmp_path, value, **kwargs):
    grants = value["instructions"]["steps"]["sync"]["v1"]["grants"]
    grants = [{**g, "args": {k: v["literal"] for k, v in g["args"].items()}} for g in grants]
    return PolicyExecutor(
        IntegrationExecutor(value, reviewed=True, **kwargs), tmp_path / "broker", {}, grants=grants
    )


def test_a_custom_binding_takes_its_origin_from_the_workflow(tmp_path):
    value = compiled(tmp_path)
    connection = value["workflow"]["spec"]["connections"][0]
    assert connection["base_url"] == "https://api.acme.test"
    # The workflow names the origin; the platform runs nothing for it.
    assert connection["managed"] is False
    assert "managed" not in connection["auth"]
    assert connection["allow_private_network"] is False
    assert connection["auth"]["connector"] == "custom"
    assert connection["auth"]["credential"] == "vault:acme/token"
    assert [e["kind"] for e in connection["auth"]["accepts"]] == ["token", "api_key", "basic"]
    assert value["connectors"]["acme"]["provider"] == "custom"
    grants = value["instructions"]["steps"]["sync"]["v1"]["grants"]
    assert grants == [
        {
            "capability": "acme.read",
            "args": {"path": {"literal": {"path": "/v1/orders"}}},
            "as": None,
        }
    ]


def test_a_grant_may_cover_the_whole_origin(tmp_path):
    value = compiled(tmp_path, can=["acme.read", "acme.write"])
    grants = value["instructions"]["steps"]["sync"]["v1"]["grants"]
    assert [g["capability"] for g in grants] == ["acme.read", "acme.write"]
    assert all(g["args"] == {} for g in grants)


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ({"uses": "custom", "auth": "secrets.acme"}, "base_url is required"),
        ({"uses": "custom", "base_url": "http://api.acme.test", "auth": "secrets.acme"}, "https"),
        (
            {"uses": "custom", "base_url": "https://api.acme.test/v1", "auth": "secrets.acme"},
            "origin only",
        ),
        (
            {"uses": "custom", "base_url": "https://u:p@api.acme.test", "auth": "secrets.acme"},
            "origin only",
        ),
        (
            {"uses": "custom", "base_url": "https://api.acme.test?x=1", "auth": "secrets.acme"},
            "origin only",
        ),
        ({"uses": "custom", "base_url": "https://api.acme.test"}, "auth is required"),
        (
            {
                "uses": "custom",
                "base_url": "https://api.acme.test",
                "auth": "secrets.acme",
                "allow_private_network": "yes",
            },
            "true or false",
        ),
        (
            {"uses": "github", "base_url": "https://api.acme.test", "auth": "secrets.acme"},
            "supports auth, uses",
        ),
        (
            {"uses": "custom", "base_url": "https://api.acme.test", "auth": "secrets.acme", "x": 1},
            "supports",
        ),
    ],
)
def test_a_custom_binding_is_an_origin_and_a_secret(tmp_path, binding, message):
    with pytest.raises(ConfigError, match=message):
        compiled(tmp_path, binding=binding)


def test_a_local_run_may_name_a_private_http_origin(tmp_path):
    value = compiled(
        tmp_path,
        binding={
            "uses": "custom",
            "base_url": "http://localhost:8080/",
            "auth": "secrets.acme",
            "allow_private_network": True,
        },
    )
    connection = value["workflow"]["spec"]["connections"][0]
    assert connection["base_url"] == "http://localhost:8080"
    assert connection["allow_private_network"] is True


@pytest.mark.parametrize("path", ["v1/orders", "/v1/../admin", "/v1?x=1", "/a//b", "/v1#f", ""])
def test_a_path_grant_is_a_clean_request_path(tmp_path, path):
    with pytest.raises(ConfigError):
        compiled(tmp_path, can=[{"acme.read": {"path": path}}])


def test_the_managed_runner_refuses_private_network_workflows(tmp_path, monkeypatch):
    import importlib

    main = importlib.import_module("outcomeci.cloud_runner.main")
    from outcomeci.cloud_runner.models import ContractError

    compiled(
        tmp_path,
        binding={
            "uses": "custom",
            "base_url": "http://10.0.0.5",
            "auth": "secrets.acme",
            "allow_private_network": True,
        },
    )
    content = (tmp_path / "orders.outcome.yaml").read_text()

    from unittest.mock import MagicMock

    client = MagicMock()
    client.claim_workflow.return_value = {
        "lease_token": "lease",
        "content": content,
        "files": {},
        "agents": [],
    }
    monkeypatch.setenv("AGENT_PRIVATE_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "_claim_or_skip", lambda claim: claim())
    with pytest.raises(ContractError, match="refuse private network access.*acme"):
        main.execute_workflow(main.Launch.__new__(main.Launch), client)


def _send(expect_header, status=200):
    def send(request):
        name, value = expect_header
        assert request.headers[name] == value
        assert request.url.host == "api.acme.test"
        return httpx.Response(status, json={"orders": [{"id": 1}], "path": request.url.path})

    return send


def test_a_token_signs_a_read_within_its_path_grant(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path)
    result = broker(
        tmp_path,
        value,
        resolver=lambda _: "secret-token",
        transport=httpx.MockTransport(_send(("Authorization", "Bearer secret-token"))),
    ).execute("acme.read", {"method": "GET", "path": "/v1/orders/7"}, step="sync")
    assert result["output"]["result"]["path"] == "/v1/orders/7"
    assert "secret-token" not in json.dumps(result)


def test_an_api_key_goes_in_the_header_the_credential_names(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path)
    credential = {
        "credential_type": "api_key",
        "configuration": {"header_name": "X-Acme-Key", "prefix": "Key"},
        "secrets": {"api_key": "k-123"},
    }
    result = broker(
        tmp_path,
        value,
        resolver=lambda _: credential,
        transport=httpx.MockTransport(_send(("X-Acme-Key", "Key k-123"))),
    ).execute("acme.read", {"method": "GET", "path": "/v1/orders"}, step="sync")
    assert "k-123" not in json.dumps(result)


def test_an_api_key_defaults_to_x_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path)
    credential = {"credential_type": "api_key", "configuration": {}, "secrets": {"api_key": "k"}}
    broker(
        tmp_path,
        value,
        resolver=lambda _: credential,
        transport=httpx.MockTransport(_send(("X-API-Key", "k"))),
    ).execute("acme.read", {"method": "GET", "path": "/v1/orders"}, step="sync")


@pytest.mark.parametrize("header", ["Cookie", "host", "Proxy-Authorization", "bad header", ""])
def test_a_credential_cannot_name_a_protected_header(tmp_path, monkeypatch, header):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path)
    credential = {
        "credential_type": "api_key",
        "configuration": {"header_name": header} if header else {"header_name": " "},
        "secrets": {"api_key": "k"},
    }
    with pytest.raises(IntegrationError, match="cannot send an api key"):
        broker(
            tmp_path,
            value,
            resolver=lambda _: credential,
            transport=httpx.MockTransport(_send(("X-API-Key", "k"))),
        ).execute("acme.read", {"method": "GET", "path": "/v1/orders"}, step="sync")


def test_a_first_party_connector_keeps_its_declared_header(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    path = tmp_path / "search.outcome.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v1",
                "trigger": "manual",
                "secrets": {"key": "vault:tavily/key"},
                "apis": {"web": {"uses": "tavily", "auth": "secrets.key"}},
                "steps": [{"sync": {"reason": "Search", "can": ["web.search"]}}],
            }
        )
    )
    value = compile_workflow(path)
    credential = {
        "credential_type": "api_key",
        "configuration": {"header_name": "X-Ignored"},
        "secrets": {"api_key": "k"},
    }

    def send(request):
        assert request.headers["Authorization"] == "Bearer k"
        assert "X-Ignored" not in request.headers
        return httpx.Response(200, json={"results": [], "usage": {}, "request_id": "r"})

    broker(
        tmp_path, value, resolver=lambda _: credential, transport=httpx.MockTransport(send)
    ).execute(
        "web.search",
        {
            "query": "q",
            "max_results": 1,
            "topic": "general",
            "search_depth": "basic",
            "include_domains": [],
            "exclude_domains": [],
        },
        step="sync",
    )


def test_a_read_outside_its_path_grant_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path)
    with pytest.raises(IntegrationError, match="path must be under /v1/orders"):
        broker(tmp_path, value, resolver=lambda _: "t").execute(
            "acme.read", {"method": "GET", "path": "/v1/customers"}, step="sync"
        )


def test_a_write_needs_its_own_grant_and_a_write_method(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path)
    with pytest.raises(IntegrationError, match="not authorized"):
        broker(tmp_path, value, resolver=lambda _: "t").execute(
            "acme.write", {"method": "POST", "path": "/v1/orders", "body": {}}, step="sync"
        )
    value = compiled(tmp_path, can=[{"acme.write": {"path": "/v1/orders"}}])
    with pytest.raises(IntegrationError):
        broker(tmp_path, value, resolver=lambda _: "t").execute(
            "acme.write", {"method": "GET", "path": "/v1/orders"}, step="sync"
        )


def test_the_agent_cannot_set_credential_headers_or_escape_the_origin(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path, can=["acme.read"])
    with pytest.raises(Exception, match="protected headers"):
        broker(tmp_path, value, resolver=lambda _: "t").execute(
            "acme.read",
            {"method": "GET", "path": "/v1", "headers": {"Authorization": "Bearer mine"}},
            step="sync",
        )
    with pytest.raises(IntegrationError):
        broker(tmp_path, value, resolver=lambda _: "t").execute(
            "acme.read", {"method": "GET", "path": "//evil.test/x"}, step="sync"
        )


def test_the_public_address_guard_applies_to_a_custom_origin(tmp_path):
    value = compiled(tmp_path)
    with pytest.raises(IntegrationError, match="non-public|resolved"):
        broker(tmp_path, value, resolver=lambda _: "t").execute(
            "acme.read", {"method": "GET", "path": "/v1/orders"}, step="sync"
        )
