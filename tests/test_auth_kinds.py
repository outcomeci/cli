"""Every credential kind a connector can accept, end to end through the executor.

The workflow names only a credential. The resolved Vault credential decides
the auth method, among the kinds the connection's connector accepts.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from lowered import compile_file

from outcomeci.integrations import IntegrationError, IntegrationExecutor, doctor

TOKEN_URL = "https://auth.example.test/token"


def workflow(tmp_path: Path, accepts: list[dict], *, echo: bool = False) -> dict:
    document = {
        "apiVersion": "outcomeci.workflow/v1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "auth"},
        "spec": {
            "triggers": {"manual": {"type": "manual"}},
            "instructions": {"workflow": {"content": "Call the API."}},
            "agents": {
                "default": {"runner": "codex"},
                "steps": {
                    "call": {
                        "instructions": {"content": "Call it."},
                        "needs": [],
                        "capabilities": ["tickets.read"],
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
                        "credential": "vault:tickets",
                        "accepts": accepts,
                    },
                }
            },
            "integrations": {
                "tickets": {
                    "connection": "tickets",
                    "access": {"mode": "schema"},
                    "operations": {
                        "read": {
                            "description": "Read",
                            "input": {"type": "object"},
                            "request": {"method": "GET", "path": "/v1/me"},
                            "response": {"expose": {"result": "body"}},
                        }
                    },
                }
            },
        },
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return compile_file(path)


class Server:
    """A mock provider: token endpoints plus the API, recording every request."""

    def __init__(self, tokens: list[dict] | None = None, api_echo: bool = False) -> None:
        self.tokens = list(tokens or [])
        self.api_echo = api_echo
        self.requests: list[httpx.Request] = []

    def token_requests(self) -> list[httpx.Request]:
        return [item for item in self.requests if item.url.host == "auth.example.test"]

    def api_requests(self) -> list[httpx.Request]:
        return [item for item in self.requests if item.url.host == "api.example.test"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "auth.example.test":
            if request.url.path.endswith("openid-configuration"):
                return httpx.Response(200, json={"token_endpoint": TOKEN_URL})
            return httpx.Response(200, json=self.tokens.pop(0))
        if self.api_echo:
            return httpx.Response(200, json={"seen": request.headers.get("Authorization", "")})
        return httpx.Response(200, json={"ok": True})


def run(compiled: dict, credential, server: Server, resolver=None, times: int = 1):
    executor = IntegrationExecutor(
        compiled,
        resolver=resolver or (lambda _reference: credential),
        transport=httpx.MockTransport(server),
    )
    return [executor.execute("tickets.read", {}, step="call") for _ in range(times)]


def typed(credential_type: str, secrets: dict, configuration: dict | None = None) -> dict:
    return {
        "credential_type": credential_type,
        "configuration": configuration or {},
        "secrets": secrets,
    }


def rsa_key() -> tuple[str, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


def decode(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def verified_claims(jwt: str, public_key: rsa.RSAPublicKey) -> dict:
    header, claims, signature = jwt.split(".")
    public_key.verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert decode(header)["alg"] == "RS256"
    return decode(claims)


TOKEN = {"kind": "token", "header": "Authorization", "scheme": "Bearer", "credential": ["value"]}


def test_a_plain_value_is_a_token(tmp_path: Path) -> None:
    server = Server(api_echo=True)
    [result] = run(workflow(tmp_path, [TOKEN]), "ghp-secret", server)
    assert server.api_requests()[0].headers["Authorization"] == "Bearer ghp-secret"
    assert "ghp-secret" not in json.dumps(result)


def test_an_auth_header_credential_authenticates_as_a_token(tmp_path: Path) -> None:
    server = Server()
    run(workflow(tmp_path, [TOKEN]), typed("auth_header", {"value": "xoxb-1"}), server)
    assert server.api_requests()[0].headers["Authorization"] == "Bearer xoxb-1"


def test_api_key_in_a_header_with_a_scheme_or_in_the_query(tmp_path: Path) -> None:
    server = Server()
    header = {"kind": "api_key", "header": "X-Key", "scheme": "Token", "credential": ["api_key"]}
    run(workflow(tmp_path, [header]), typed("api_key", {"api_key": "k-1"}), server)
    assert server.api_requests()[0].headers["X-Key"] == "Token k-1"

    server = Server()
    query = {"kind": "api_key", "query": "key", "credential": ["api_key"]}
    run(workflow(tmp_path / "q", [query]), "k-2", server)
    assert server.api_requests()[0].url.params["key"] == "k-2"


def test_basic_encodes_username_and_password(tmp_path: Path) -> None:
    server = Server(api_echo=True)
    basic = {"kind": "basic", "credential": ["username", "password"]}
    [result] = run(
        workflow(tmp_path, [basic]),
        typed("basic", {"username": "izzy", "password": "pw"}),
        server,
    )
    expected = base64.b64encode(b"izzy:pw").decode()
    assert server.api_requests()[0].headers["Authorization"] == f"Basic {expected}"
    assert expected not in json.dumps(result)


def oauth2(**overrides) -> dict:
    return {
        "kind": "oauth2",
        "credential": ["client_id", "client_secret"],
        "token_url": TOKEN_URL,
        "grant_types": ["client_credentials"],
        "scopes": ["read", "write"],
        "audience": None,
        "client_auth": "basic",
        "rotates_refresh_token": False,
        **overrides,
    }


def test_oauth2_client_credentials_exchanges_once_and_redacts(tmp_path: Path) -> None:
    server = Server(tokens=[{"access_token": "at-1", "expires_in": 3600}], api_echo=True)
    credential = typed("oauth2", {"client_secret": "cs"}, {"client_id": "cid"})
    results = run(workflow(tmp_path, [oauth2()]), credential, server, times=2)
    [exchange] = server.token_requests()
    form = parse_qs(exchange.content.decode())
    assert form == {"grant_type": ["client_credentials"], "scope": ["read write"]}
    assert exchange.headers["Authorization"] == "Basic " + base64.b64encode(b"cid:cs").decode()
    assert [item.headers["Authorization"] for item in server.api_requests()] == ["Bearer at-1"] * 2
    assert "at-1" not in json.dumps(results)


def test_oauth2_client_auth_in_the_body(tmp_path: Path) -> None:
    server = Server(tokens=[{"access_token": "at-1"}])
    credential = typed("oauth2", {"client_secret": "cs"}, {"client_id": "cid"})
    run(workflow(tmp_path, [oauth2(client_auth="body", scopes=[])]), credential, server)
    form = parse_qs(server.token_requests()[0].content.decode())
    assert form["client_id"] == ["cid"] and form["client_secret"] == ["cs"]
    assert "Authorization" not in server.token_requests()[0].headers


def test_oauth2_rotation_writes_the_new_refresh_token_back_before_use(tmp_path: Path) -> None:
    stored = typed(
        "oauth2",
        {"client_secret": "cs", "refresh_token": "rt-1"},
        {"client_id": "cid", "grant_type": "refresh_token"},
    )
    written: list[tuple[str, dict]] = []

    class Vault:
        def __call__(self, _reference):
            return json.loads(json.dumps(stored))

        def rotate(self, reference, secrets):
            written.append((reference, secrets))
            stored["secrets"].update(secrets)

    server = Server(
        tokens=[
            {"access_token": "at-1", "refresh_token": "rt-2", "expires_in": 0},
            {"access_token": "at-2", "refresh_token": "rt-3", "expires_in": 0},
        ]
    )
    entry = oauth2(grant_types=["refresh_token"], rotates_refresh_token=True, scopes=[])
    entry["credential"] = ["client_id", "client_secret", "refresh_token"]
    run(workflow(tmp_path, [entry]), None, server, resolver=Vault(), times=2)
    sent = [parse_qs(item.content.decode())["refresh_token"] for item in server.token_requests()]
    assert sent == [["rt-1"], ["rt-2"]]
    assert written == [
        ("vault:tickets", {"refresh_token": "rt-2"}),
        ("vault:tickets", {"refresh_token": "rt-3"}),
    ]


def test_a_rotating_provider_refuses_to_refresh_where_it_cannot_save(tmp_path: Path) -> None:
    server = Server(tokens=[{"access_token": "at-1", "refresh_token": "rt-2"}])
    entry = oauth2(grant_types=["refresh_token"], rotates_refresh_token=True)
    credential = typed(
        "oauth2", {"client_secret": "cs", "refresh_token": "rt-1"}, {"client_id": "cid"}
    )
    with pytest.raises(IntegrationError, match="cannot save the new one"):
        run(workflow(tmp_path, [entry]), credential, server)
    assert server.requests == []


def test_interactive_authorization_metadata_reuses_unattended_refresh(tmp_path: Path) -> None:
    """Browser consent is upstream; runners only receive the Vault refresh credential."""
    stored = typed(
        "oauth2",
        {"client_secret": "app-secret", "refresh_token": "refresh-before"},
        {"client_id": "app-id", "grant_type": "refresh_token"},
    )
    saved = []

    class Vault:
        def __call__(self, _reference):
            return stored

        def rotate(self, reference, secrets):
            saved.append((reference, secrets))
            stored["secrets"].update(secrets)

    entry = oauth2(
        authorization_url="https://auth.example.test/authorize",
        pkce=True,
        grant_types=["refresh_token"],
        rotates_refresh_token=True,
        scopes=["tweet.read", "tweet.write", "users.read", "offline.access"],
    )
    entry["credential"] = ["client_id", "client_secret", "refresh_token"]
    server = Server(
        tokens=[
            {
                "access_token": "access-after",
                "refresh_token": "refresh-after",
                "expires_in": 3600,
            }
        ]
    )
    results = run(workflow(tmp_path, [entry]), None, server, resolver=Vault(), times=2)
    [exchange] = server.token_requests()
    assert str(exchange.url) == TOKEN_URL
    assert parse_qs(exchange.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-before"],
        "scope": ["tweet.read tweet.write users.read offline.access"],
    }
    assert saved == [("vault:tickets", {"refresh_token": "refresh-after"})]
    assert len(server.api_requests()) == 2
    for secret in ("app-secret", "refresh-before", "refresh-after", "access-after"):
        assert secret not in json.dumps(results)


def test_a_token_endpoint_answer_without_a_token_fails(tmp_path: Path) -> None:
    server = Server(tokens=[{"ok": False, "error": "invalid_refresh_token"}])
    credential = typed("oauth2", {"client_secret": "cs"}, {"client_id": "cid"})
    with pytest.raises(IntegrationError, match="no access token"):
        run(workflow(tmp_path, [oauth2()]), credential, server)
    assert server.api_requests() == []


def test_oidc_discovers_the_token_endpoint_from_the_credential_issuer(tmp_path: Path) -> None:
    server = Server(tokens=[{"access_token": "at-1"}])
    entry = {
        "kind": "oidc",
        "credential": ["issuer_url", "client_id", "client_secret"],
        "issuer": None,
        "discovery_url": None,
        "scopes": [],
        "audience": "api://tickets",
    }
    credential = typed(
        "oidc",
        {"client_secret": "cs"},
        {"client_id": "cid", "issuer_url": "https://auth.example.test/tenant"},
    )
    run(workflow(tmp_path, [entry]), credential, server)
    discovery, exchange = server.token_requests()
    assert discovery.url.path == "/tenant/.well-known/openid-configuration"
    assert parse_qs(exchange.content.decode())["audience"] == ["api://tickets"]
    assert server.api_requests()[0].headers["Authorization"] == "Bearer at-1"


def test_jwt_bearer_signs_an_assertion_and_exchanges_it(tmp_path: Path) -> None:
    pem, public = rsa_key()
    server = Server(tokens=[{"access_token": "at-1"}], api_echo=True)
    entry = {
        "kind": "jwt_bearer",
        "credential": ["issuer", "subject", "private_key"],
        "token_url": TOKEN_URL,
        "audience": None,
        "scopes": ["tickets.read"],
        "algorithm": "RS256",
    }
    credential = typed(
        "jwt_bearer", {"private_key": pem}, {"issuer": "svc@example", "subject": "izzy"}
    )
    [result] = run(workflow(tmp_path, [entry]), credential, server)
    form = parse_qs(server.token_requests()[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = verified_claims(form["assertion"][0], public)
    assert claims["iss"] == "svc@example" and claims["sub"] == "izzy"
    assert claims["aud"] == TOKEN_URL and claims["scope"] == "tickets.read"
    assert form["assertion"][0] not in json.dumps(result)


def test_app_installation_signs_as_the_app_and_caches_the_installation_token(
    tmp_path: Path,
) -> None:
    pem, public = rsa_key()
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    server = Server(tokens=[{"token": "ghs_1", "expires_at": expires}], api_echo=True)
    entry = {
        "kind": "app_installation",
        "credential": ["app_id", "installation_id", "private_key"],
        "token_url": "https://auth.example.test/app/installations/{installation_id}/access_tokens",
        "method": "POST",
        "headers": {"Accept": "application/vnd.github+json"},
        "algorithm": "RS256",
        "jwt_lifetime_seconds": 600,
        "token_field": "token",
        "expires_field": "expires_at",
        "scheme": "Bearer",
    }
    credential = typed(
        "app_installation", {"private_key": pem}, {"app_id": "123", "installation_id": "456"}
    )
    results = run(workflow(tmp_path, [entry]), credential, server, times=2)
    [exchange] = server.token_requests()
    assert exchange.method == "POST"
    assert exchange.url.path == "/app/installations/456/access_tokens"
    assert exchange.headers["Accept"] == "application/vnd.github+json"
    claims = verified_claims(exchange.headers["Authorization"].removeprefix("Bearer "), public)
    assert claims["iss"] == "123"
    assert claims["iat"] <= time.time() - 59
    assert claims["exp"] - claims["iat"] == 600
    assert [item.headers["Authorization"] for item in server.api_requests()] == ["Bearer ghs_1"] * 2
    assert "ghs_1" not in json.dumps(results)


def test_a_kind_the_connector_does_not_accept_fails_before_any_request(tmp_path: Path) -> None:
    server = Server()
    credential = typed("oauth2", {"client_secret": "cs"}, {"client_id": "cid"})
    with pytest.raises(IntegrationError) as raised:
        run(workflow(tmp_path, [TOKEN]), credential, server)
    assert raised.value.code == "integration.credential_kind_unsupported"
    assert str(raised.value) == (
        "tickets cannot use a oauth2 credential (Vault entry tickets); it accepts: token"
    )
    assert server.requests == []


def test_an_incomplete_credential_names_what_is_missing(tmp_path: Path) -> None:
    server = Server()
    with pytest.raises(IntegrationError, match="missing client_id"):
        run(workflow(tmp_path, [oauth2()]), typed("oauth2", {"client_secret": "cs"}), server)
    assert server.requests == []


def test_doctor_reports_the_kind_a_credential_authenticates_as(tmp_path: Path) -> None:
    compiled = workflow(tmp_path, [TOKEN, oauth2()])
    report = doctor(
        compiled,
        resolver=lambda _reference: typed("oauth2", {"client_secret": "cs"}, {"client_id": "cid"}),
    )
    shape = next(check for check in report["checks"] if check["check"] == "credential_shape")
    assert shape == {
        "check": "credential_shape",
        "connection": "tickets",
        "status": "pass",
        "detail": "authenticates as oauth2",
    }
    report = doctor(
        compiled, resolver=lambda _reference: typed("basic", {"username": "u", "password": "p"})
    )
    assert report["ok"] is False


def test_github_refresh_requests_json_and_persists_rotation(tmp_path: Path) -> None:
    from outcomeci_connectors.providers.github import PROVIDER

    entry = next(
        item for item in PROVIDER.contract()["auth"]["accepts"] if item["kind"] == "oauth2"
    )
    stored = typed(
        "oauth2",
        {"client_secret": "cs", "refresh_token": "rt-1"},
        {"client_id": "cid", "grant_type": "refresh_token"},
    )
    requests = []
    writes = []

    class Vault:
        def __call__(self, _reference):
            return json.loads(json.dumps(stored))

        def rotate(self, reference, secrets):
            writes.append((reference, secrets))
            stored["secrets"].update(secrets)

    def server(request):
        requests.append(request)
        if request.url.host == "github.com":
            assert request.headers["Accept"] == "application/json"
            assert "Authorization" not in request.headers
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["cid"]
            assert form["client_secret"] == ["cs"]
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == ["rt-1"]
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-2",
                    "expires_in": 28800,
                    "scope": "repo",
                },
            )
        assert writes == [("vault:tickets", {"refresh_token": "rt-2"})]
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"ok": True})

    run(workflow(tmp_path, [entry]), None, server, resolver=Vault(), times=2)
    assert len([request for request in requests if request.url.host == "github.com"]) == 1


@pytest.mark.parametrize("ok", [True, False])
def test_slack_refresh_uses_bot_token_and_saves_rotation(ok):
    from outcomeci_connectors.providers.slack import PROVIDER

    from outcomeci.auth import Authenticator, AuthError

    writes = []
    auth = Authenticator(rotate=lambda reference, secrets: writes.append((reference, secrets)))
    contract = {**PROVIDER.contract()["auth"], "connector": "slack", "credential": "vault:slack"}
    credential = typed(
        "oauth2",
        {"client_secret": "cs", "refresh_token": "rt-1"},
        {"client_id": "cid", "grant_type": "refresh_token"},
    )

    def server(request):
        assert request.url == "https://slack.com/api/oauth.v2.access"
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"cid:cs").decode()
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["rt-1"],
        }
        return httpx.Response(
            200,
            json={
                "ok": ok,
                "access_token": "bot-token",
                "refresh_token": "rt-2",
                "token_type": "bot",
                "expires_in": 43200,
            },
        )

    headers = {}
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        if ok:
            auth.apply(client, contract, credential, headers, {})
            assert headers["Authorization"] == "Bearer bot-token"
            assert writes == [("vault:slack", {"refresh_token": "rt-2"})]
        else:
            with pytest.raises(AuthError):
                auth.apply(client, contract, credential, headers, {})
            assert not writes
            assert not headers
