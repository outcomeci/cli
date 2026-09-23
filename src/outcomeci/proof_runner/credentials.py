"""Vault and connection-credential actions for vault-credentials-v1.

Exercises every connection auth type the product supports (api_key, basic,
bearer, oauth2 client_credentials, oauth2 refresh_token, jwt_bearer) against
a local mock authorization server, plus local Vault rotation and a mocked
cloud Vault, so a regression in credential resolution, header/token
construction, or rotation handling fails a proof instead of shipping quietly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa

from ..config import compile_workflow
from ..integrations import IntegrationExecutor, environment_resolver, local_credential_resolver
from ..local_vault import put as put_vault
from ..local_vault import resolve as resolve_vault
from ..process import ExecutionError

CONNECTION_AUTH_TYPES = {
    "api_key",
    "basic",
    "bearer",
    "oauth2_client_credentials",
    "oauth2_refresh_token",
    "jwt_bearer",
}


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_credential(value: Any) -> str:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        if isinstance(value, dict)
        else str(value)
    )


def _hash_credential(value: Any) -> str:
    return hashlib.sha256(_encode_credential(value).encode()).hexdigest()


def _credential_state(context: dict[str, Any]) -> dict[str, Any]:
    return context.setdefault("vault_credentials", {})


# ── Local Vault: store, rotate, resolve ─────────────────────────────────────


def put_credential(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    path, value = str(request["path"]), request["value"]
    put_vault(root, path, _encode_credential(value))
    _credential_state(context)[path] = {
        "current_sha256": _hash_credential(value),
        "retired_sha256": [],
    }
    return {"status": "stored", "path": path}


def rotate_credential(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    path, value = str(request["path"]), request["value"]
    state = _credential_state(context)
    if path not in state:
        raise ExecutionError(f"cannot rotate {path}: no credential was previously stored")
    put_vault(root, path, _encode_credential(value))
    state[path] = {
        "current_sha256": _hash_credential(value),
        "retired_sha256": [*state[path]["retired_sha256"], state[path]["current_sha256"]],
    }
    return {"status": "rotated", "path": path}


def resolve_credential(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    path = str(request["path"])
    state = _credential_state(context)
    if path not in state:
        raise ExecutionError(f"cannot resolve {path}: no credential was stored")
    resolved = resolve_vault(root, f"vault:{path}")
    matches = _hash_credential(resolved) == state[path]["current_sha256"]
    return {"status": "resolved", "path": path, "matches_current": matches}


def generate_jwt_credential(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    path = str(request["path"])
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    value = {
        "private_key": private_pem,
        "issuer": str(request["issuer"]),
        "subject": str(request.get("subject", request["issuer"])),
        "algorithm": "RS256",
    }
    # Unlike every other credential type here, this secret is code-generated
    # rather than operator-supplied, so it never carries the
    # oci_vault_proof_ marker credentials.never_exposed otherwise greps for.
    # Record one unbroken base64 line of the key body (workspace-local
    # state, never written to the ledger/report) so that check can fall
    # back to an exact-match scan for this credential too. A single line
    # with no newlines survives JSON-encoding unchanged (unlike the full,
    # multi-line PEM), so the scan still catches a leak that went through
    # json.dumps on its way into the ledger.
    key_body_line = private_pem.splitlines()[1]
    context.setdefault("generated_secrets", []).append(key_body_line)
    return put_credential(root, context, {"path": path, "value": value})


def resolve_env_credential(request: dict[str, Any]) -> dict[str, Any]:
    name = str(request["env_var"])
    # A custom human transport's credential lives in a host environment
    # variable the workflow author declares (see docs/workflow.md), not in a
    # vault; simulate that host configuration here rather than requiring
    # whoever runs this proof to have set it beforehand.
    os.environ.setdefault(name, "oci_vault_proof_env_value")
    resolved = environment_resolver(f"env:{name}")
    expected = os.environ[name]
    matches = resolved == expected or resolved == {"value": expected}
    return {"status": "resolved", "matches": matches}


# ── Connection auth types, against a local mock authorization server ───────


def _configure_credential_connection(
    root: Path, auth_type: str, credential_path: str, base_url: str
) -> None:
    path = root / "outcome.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = workflow["spec"]
    auth: dict[str, Any] = {"credential": f"vault:{credential_path}"}
    if auth_type == "api_key":
        auth.update({"type": "api_key", "header": "X-API-Key"})
    elif auth_type == "basic":
        auth.update({"type": "basic"})
    elif auth_type == "bearer":
        auth.update({"type": "bearer"})
    elif auth_type == "oauth2_client_credentials":
        auth.update(
            {"type": "oauth2", "token_url": f"{base_url}/token", "grant_type": "client_credentials"}
        )
    elif auth_type == "oauth2_refresh_token":
        auth.update(
            {"type": "oauth2", "token_url": f"{base_url}/token", "grant_type": "refresh_token"}
        )
    elif auth_type == "jwt_bearer":
        auth.update({"type": "jwt_bearer", "token_url": f"{base_url}/token"})
    else:
        raise ExecutionError(f"unsupported credential auth_type {auth_type}")
    spec["connections"] = [
        {
            "ref": "credential_check",
            "provider": "http",
            "base_url": base_url,
            "allow_private_network": True,
            "auth": auth,
        }
    ]
    spec["integrations"] = {
        "credential_check": {
            "connection": "credential_check",
            "access": {"mode": "schema"},
            "operations": {
                "verify": {
                    "description": "Verify a credential type authenticates correctly.",
                    "policy": {
                        "side_effect": "read",
                        "approval": "none",
                        "idempotency": "supported",
                    },
                    "input": {"type": "object", "additionalProperties": False},
                    "request": {"method": "GET", "path": "/verify"},
                    "response": {"expose": {"accepted": "body.accepted"}},
                }
            },
        }
    }
    spec["agents"]["phases"]["intake"]["integrations"] = [
        {"type": "api", "capability": "credential_check.verify"}
    ]
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")


def _mock_authorization_server(auth_type: str, expected: Any) -> ThreadingHTTPServer:
    minted: dict[str, str] = {}

    def resource_authorized(headers: Any) -> bool:
        header = headers.get("Authorization", "")
        if auth_type == "api_key":
            return headers.get("X-API-Key") == expected.get("value")
        if auth_type == "basic":
            pair = f"{expected.get('username', '')}:{expected.get('password', '')}"
            return header == f"Basic {base64.b64encode(pair.encode()).decode()}"
        if auth_type == "bearer":
            return header == f"Bearer {expected.get('value')}"
        return bool(minted.get("token")) and header == f"Bearer {minted['token']}"

    def jwt_verified(assertion: str) -> bool:
        try:
            header_b64, payload_b64, signature_b64 = assertion.split(".")
            payload = json.loads(_b64url_decode(payload_b64))
            private_key = serialization.load_pem_private_key(
                expected["private_key"].encode(), password=None
            )
            private_key.public_key().verify(
                _b64url_decode(signature_b64),
                f"{header_b64}.{payload_b64}".encode(),
                asym_padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            return False
        return payload.get("iss") == expected.get("issuer")

    def token_authorized(form: dict[str, str], authorization_header: str) -> bool:
        if auth_type == "oauth2_client_credentials":
            pair = f"{expected.get('client_id', '')}:{expected.get('client_secret', '')}"
            return (
                form.get("grant_type") == "client_credentials"
                and authorization_header == f"Basic {base64.b64encode(pair.encode()).decode()}"
            )
        if auth_type == "oauth2_refresh_token":
            return form.get("grant_type") == "refresh_token" and form.get(
                "refresh_token"
            ) == expected.get("refresh_token")
        if auth_type == "jwt_bearer":
            return form.get(
                "grant_type"
            ) == "urn:ietf:params:oauth:grant-type:jwt-bearer" and jwt_verified(
                form.get("assertion", "")
            )
        return False

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler naming)
            if self.path != "/verify":
                self._send(404, {})
                return
            accepted = resource_authorized(self.headers)
            self._send(200 if accepted else 401, {"accepted": accepted})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/token":
                self._send(404, {})
                return
            length = int(self.headers.get("Content-Length", 0))
            form = {
                key: values[0]
                for key, values in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()
            }
            if not token_authorized(form, self.headers.get("Authorization", "")):
                self._send(401, {"error": "invalid_grant"})
                return
            minted["token"] = f"tok_{os.urandom(12).hex()}"
            self._send(200, {"access_token": minted["token"]})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def authenticate_connection(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    auth_type = str(request["auth_type"])
    if auth_type not in CONNECTION_AUTH_TYPES:
        raise ExecutionError(f"unsupported credential auth_type {auth_type}")
    credential_path = str(request["credential_path"])
    expected = resolve_vault(root, f"vault:{credential_path}")
    server = _mock_authorization_server(auth_type, expected)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _configure_credential_connection(
            root, auth_type, credential_path, f"http://127.0.0.1:{port}"
        )
        result = IntegrationExecutor(
            compile_workflow(root / "outcome.yml"),
            resolver=local_credential_resolver(root),
            transport=httpx.HTTPTransport(),
        ).execute("credential_check.verify", {}, phase="intake")
    finally:
        server.shutdown()
        thread.join(timeout=2)
    if result.get("output") != {"accepted": True}:
        raise ExecutionError(f"credential type {auth_type} did not authenticate")
    return {"status": "authenticated", "auth_type": auth_type}
