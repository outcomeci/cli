"""Authenticate a connector request with whatever kind of credential the Vault holds.

A workflow names a credential, never an auth method. A connection carries the
credential kinds its connector accepts, in order of preference, with the
parameters the connector fixes for each (see `outcomeci_connectors.auth`). At
request time the resolved credential decides which accepted kind applies: a
plain value is a token, and a typed credential carries its `credential_type`.
A credential whose kind the connector does not accept fails before any request
is sent.

Kinds that exchange a credential for a short-lived token (oauth2, oidc,
jwt_bearer, app_installation) cache the token for the life of the caller. A
provider that rotates its refresh token revokes the one a refresh used, so the
new one is written back through the resolver's `rotate` before it is used.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .process import ExecutionError

# A Vault `credential_type` and the connector kind it authenticates as.
KIND_OF_TYPE = {
    "auth_header": "token",
    "token": "token",
    "bearer": "token",
    "api_key": "api_key",
    "basic": "basic",
    "oauth2": "oauth2",
    "oidc": "oidc",
    "jwt_bearer": "jwt_bearer",
    "app_installation": "app_installation",
}
EXCHANGED = {"oauth2", "oidc", "jwt_bearer", "app_installation"}
# Refresh a cached token this long before the provider says it expires.
EXPIRY_MARGIN_SECONDS = 60
DEFAULT_TOKEN_SECONDS = 300
Rotate = Callable[[str, dict[str, str]], None]


class AuthError(ExecutionError):
    """A credential cannot authenticate this connector; nothing was sent."""

    def __init__(self, code: str, message: str, *, category: str = "authorization"):
        super().__init__(message, False)
        self.code = code
        self.category = category


@dataclass
class Credential:
    """A resolved Vault credential: its declared type, if any, and its fields."""

    credential_type: str | None
    configuration: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    def fields(self) -> dict[str, Any]:
        return {**self.configuration, **self.secrets}

    def value(self) -> str:
        return str(
            self.secrets.get("value")
            or self.secrets.get("api_key")
            or self.secrets.get("token")
            or ""
        )

    def sensitive(self) -> list[str]:
        return [str(item) for item in self.secrets.values() if item]


def parse(resolved: Mapping[str, Any] | str | None) -> Credential:
    """Read what a resolver returned: a plain value, a typed credential, or fields."""
    if resolved is None:
        return Credential(None)
    if isinstance(resolved, str):
        return Credential(None, {}, {"value": resolved})
    if isinstance(resolved.get("secrets"), Mapping):
        configuration = resolved.get("configuration")
        return Credential(
            str(resolved["credential_type"]) if resolved.get("credential_type") else None,
            dict(configuration) if isinstance(configuration, Mapping) else {},
            {str(key): str(value) for key, value in resolved["secrets"].items()},
        )
    return Credential(None, {}, {str(key): str(value) for key, value in resolved.items()})


def _untyped_kind(credential: Credential, kinds: list[str]) -> str | None:
    """The kind an untyped credential's fields fit, in the connector's order."""
    held = set(credential.secrets)
    for kind in kinds:
        if kind in {"token", "api_key"} and held & {"value", "api_key", "token"}:
            return kind
        if kind == "basic" and {"username", "password"} <= held:
            return kind
    return None


def select(auth: Mapping[str, Any], credential: Credential, reference: str) -> dict[str, Any]:
    """The accepted entry this credential authenticates as, or a clear refusal."""
    accepts = list(auth["accepts"])
    kinds = [entry["kind"] for entry in accepts]
    connector = auth.get("connector", "this connector")
    entry_name = reference.removeprefix("vault:")
    if credential.credential_type is not None:
        kind = KIND_OF_TYPE.get(credential.credential_type)
        found = next((entry for entry in accepts if entry["kind"] == kind), None)
        described = f"a {credential.credential_type} credential"
    else:
        kind = _untyped_kind(credential, kinds)
        found = next((entry for entry in accepts if entry["kind"] == kind), None)
        described = "a plain value" if "value" in credential.secrets else "this credential"
    if found is None:
        raise AuthError(
            "integration.credential_kind_unsupported",
            f"{connector} cannot use {described} (Vault entry {entry_name}); "
            f"it accepts: {', '.join(kinds)}",
            category="configuration",
        )
    missing = [
        name
        for name in found.get("credential", [])
        if name not in credential.fields()
        and not (name in {"value", "api_key"} and credential.value())
        and not (name == "issuer_url" and found.get("discovery_url"))
        and not (name == "refresh_token" and _grant(found, credential) != "refresh_token")
    ]
    if missing:
        raise AuthError(
            "integration.credential_incomplete",
            f"Vault entry {entry_name} is missing {', '.join(missing)} for {connector} "
            f"{found['kind']} auth",
            category="configuration",
        )
    return found


def _grant(entry: Mapping[str, Any], credential: Credential) -> str:
    grants = list(entry.get("grant_types") or ["client_credentials"])
    configured = credential.configuration.get("grant_type")
    if configured in grants:
        return str(configured)
    if "refresh_token" in grants and credential.secrets.get("refresh_token"):
        return "refresh_token"
    return grants[0]


def _https(url: Any, name: str) -> str:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise AuthError(
            "integration.auth_endpoint_invalid",
            f"{name} must be an https URL",
            category="configuration",
        )
    return url


def _scopes(entry: Mapping[str, Any], credential: Credential) -> str | None:
    scopes = list(entry.get("scopes") or [])
    configured = credential.configuration.get("scopes")
    if configured is None:
        configured = credential.configuration.get("scope")
    optional = entry.get("optional_scopes") or []
    if optional:
        selected = scopes if configured is None else configured
        if isinstance(selected, str):
            selected = selected.split()
        if (
            not isinstance(selected, list)
            or not selected
            or any(not isinstance(item, str) for item in selected)
            or len(selected) != len(set(selected))
            or not set(scopes) <= set(selected)
            or not set(selected) <= set(scopes + list(optional))
        ):
            raise AuthError(
                "integration.invalid_scopes",
                "Choose credential scopes supported by the connector and approved for your app",
                category="configuration",
            )
        scopes = selected
    elif not scopes and configured:
        scopes = configured if isinstance(configured, list) else str(configured).split()
    separator = entry.get("scope_separator", " ") if entry.get("kind") == "oauth2" else " "
    if separator not in (" ", ","):
        raise AuthError(
            "integration.invalid_scope_separator",
            "OAuth scope_separator must be a space or comma",
            category="configuration",
        )
    return separator.join(str(item) for item in scopes) or None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_rs256(claims: Mapping[str, Any], private_key_pem: str) -> str:
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except ValueError as exc:
        raise AuthError(
            "integration.credential_invalid",
            "the credential's private_key is not a valid PEM private key",
            category="configuration",
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError(
            "integration.credential_invalid",
            "the credential's private_key must be an RSA key",
            category="configuration",
        )
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}"
        f".{_b64url(json.dumps(dict(claims), separators=(',', ':')).encode())}"
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _token_response(response: httpx.Response, connector: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AuthError(
            "integration.token_exchange_failed",
            f"{connector} token endpoint returned HTTP {response.status_code}",
            category="authorization",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError(
            "integration.token_exchange_failed",
            f"{connector} token endpoint returned no JSON",
        ) from exc
    if not isinstance(body, dict) or (connector == "slack" and body.get("ok") is not True):
        raise AuthError("integration.token_exchange_failed", f"{connector} returned no token")
    return body


def _expiry(seconds: Any) -> float:
    try:
        lifetime = float(seconds)
    except (TypeError, ValueError):
        lifetime = DEFAULT_TOKEN_SECONDS
    return time.time() + max(lifetime - EXPIRY_MARGIN_SECONDS, 0)


class Authenticator:
    """Applies a connection's auth to a request, caching exchanged tokens.

    `rotate(reference, secrets)` persists secret fields a provider replaced
    (a rotated refresh token); a connector that rotates cannot refresh
    without it.
    """

    def __init__(self, rotate: Rotate | None = None) -> None:
        self.rotate = rotate
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self.derived: list[str] = []

    def apply(
        self,
        client: httpx.Client,
        auth: Mapping[str, Any],
        resolved: Mapping[str, Any] | str | None,
        headers: dict[str, str],
        query: dict[str, Any],
    ) -> list[str]:
        """Authenticate the request in place; return every secret to redact."""
        if [entry["kind"] for entry in auth["accepts"]] == ["none"]:
            return []
        reference = str(auth["credential"])
        credential = parse(resolved)
        entry = select(auth, credential, reference)
        kind = entry["kind"]
        if entry.get("optional_scopes"):
            _scopes(entry, credential)
        sensitive = credential.sensitive()
        if kind == "token":
            value = credential.value()
            scheme = entry.get("scheme")
            headers[entry.get("header") or "Authorization"] = (
                f"{scheme} {value}" if scheme else value
            )
        elif kind == "api_key":
            value = credential.value()
            if entry.get("header"):
                scheme = entry.get("scheme")
                headers[entry["header"]] = f"{scheme} {value}" if scheme else value
            else:
                query[entry["query"]] = value
        elif kind == "basic":
            pair = (
                f"{credential.secrets.get('username', '')}:{credential.secrets.get('password', '')}"
            )
            encoded = base64.b64encode(pair.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
            sensitive.append(encoded)
        else:
            token = self._exchanged(client, auth, entry, credential, reference)
            scheme = entry.get("scheme") or "Bearer"
            headers["Authorization"] = f"{scheme} {token}"
            sensitive.append(token)
        sensitive.extend(self.derived)
        return [item for item in sensitive if item]

    def _exchanged(
        self,
        client: httpx.Client,
        auth: Mapping[str, Any],
        entry: Mapping[str, Any],
        credential: Credential,
        reference: str,
    ) -> str:
        key = (reference, entry["kind"])
        cached = self._cache.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
        connector = str(auth.get("connector", "connector"))
        kind = entry["kind"]
        if kind == "app_installation":
            token, expires = self._app_installation(client, entry, credential, connector)
        else:
            token, expires = self._oauth(client, entry, credential, connector, reference)
        self._cache[key] = (token, expires)
        self.derived.append(token)
        return token

    def _oauth(
        self,
        client: httpx.Client,
        entry: Mapping[str, Any],
        credential: Credential,
        connector: str,
        reference: str,
    ) -> tuple[str, float]:
        kind = entry["kind"]
        fields = credential.fields()
        if kind == "oidc":
            discovery = entry.get("discovery_url")
            if not discovery:
                issuer = _https(fields.get("issuer_url") or entry.get("issuer"), "issuer_url")
                discovery = issuer.rstrip("/") + "/.well-known/openid-configuration"
            found = _token_response(client.get(_https(discovery, "discovery_url")), connector)
            token_url = _https(found.get("token_endpoint"), "token_endpoint")
        else:
            token_url = _https(entry.get("token_url") or fields.get("token_url"), "token_url")
        audience = entry.get("audience") or fields.get("audience")
        scope = _scopes(entry, credential)
        request_auth = None
        rotated_before = False
        if kind == "jwt_bearer":
            now = int(time.time())
            claims = {
                "iss": fields.get("issuer", ""),
                "aud": audience or token_url,
                "iat": now,
                "exp": now + 300,
            }
            if fields.get("subject"):
                claims["sub"] = fields["subject"]
            if scope:
                claims["scope"] = scope
            assertion = sign_rs256(claims, str(fields.get("private_key", "")))
            self.derived.append(assertion)
            data: dict[str, str] = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        else:
            grant = _grant(entry, credential) if kind == "oauth2" else "client_credentials"
            data = {"grant_type": grant}
            if grant == "refresh_token":
                refresh = credential.secrets.get("refresh_token")
                if not refresh:
                    raise AuthError(
                        "integration.credential_incomplete",
                        f"the refresh_token grant needs a refresh_token in {reference}",
                        category="configuration",
                    )
                if entry.get("rotates_refresh_token") and self.rotate is None:
                    raise AuthError(
                        "integration.rotation_unavailable",
                        f"{connector} rotates its refresh token, and this run cannot save "
                        f"the new one to {reference.removeprefix('vault:')}",
                        category="configuration",
                    )
                data["refresh_token"] = refresh
                rotated_before = bool(entry.get("rotates_refresh_token"))
            client_id = str(fields.get("client_id", ""))
            client_secret = str(credential.secrets.get("client_secret", ""))
            if entry.get("client_auth") == "body":
                data["client_id"] = client_id
                data["client_secret"] = client_secret
            else:
                request_auth = (client_id, client_secret)
            if scope and connector != "slack":
                data["scope"] = scope
            if audience:
                data["audience"] = str(audience)
        response = client.post(
            token_url, data=data, auth=request_auth, headers={"Accept": "application/json"}
        )
        body = _token_response(response, connector)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError(
                "integration.token_exchange_failed",
                f"{connector} token endpoint returned no access token",
            )
        new_refresh = body.get("refresh_token")
        if (
            rotated_before
            and isinstance(new_refresh, str)
            and new_refresh != data.get("refresh_token")
        ):
            self.derived.append(new_refresh)
            assert self.rotate is not None
            self.rotate(reference, {"refresh_token": new_refresh})
            credential.secrets["refresh_token"] = new_refresh
        return token, _expiry(body.get("expires_in"))

    def _app_installation(
        self,
        client: httpx.Client,
        entry: Mapping[str, Any],
        credential: Credential,
        connector: str,
    ) -> tuple[str, float]:
        fields = credential.fields()
        now = int(time.time()) - 60
        lifetime = int(entry.get("jwt_lifetime_seconds") or 600)
        claims = {"iss": str(fields.get("app_id", "")), "iat": now, "exp": now + lifetime}
        assertion = sign_rs256(claims, str(fields.get("private_key", "")))
        self.derived.append(assertion)
        installation = quote(str(fields.get("installation_id", "")), safe="")
        token_url = _https(
            str(entry["token_url"]).replace("{installation_id}", installation), "token_url"
        )
        headers = {str(key): str(value) for key, value in (entry.get("headers") or {}).items()}
        headers["Authorization"] = f"Bearer {assertion}"
        response = client.request(entry.get("method") or "POST", token_url, headers=headers)
        body = _token_response(response, connector)
        token = body.get(entry.get("token_field") or "token")
        if not isinstance(token, str) or not token:
            raise AuthError(
                "integration.token_exchange_failed",
                f"{connector} returned no installation token",
            )
        expires = body.get(entry.get("expires_field") or "expires_at")
        try:
            at = datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
            expiry = at - EXPIRY_MARGIN_SECONDS
        except ValueError:
            expiry = _expiry(None)
        return token, expiry


def shape_check(
    auth: Mapping[str, Any], resolved: Mapping[str, Any] | str | None
) -> tuple[bool, str]:
    """Whether a resolved credential fits one of the connector's accepted kinds."""
    try:
        entry = select(auth, parse(resolved), str(auth.get("credential", "")))
    except AuthError as exc:
        return False, str(exc)
    return True, f"authenticates as {entry['kind']}"
