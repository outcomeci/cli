"""Cloud Vault actions against a local mock of the OutcomeCI Cloud API.

Runs cloud.py's real vault_request()/session-refresh code against a small
mock server instead of live cloud infrastructure, so this proof stays
deterministic and network-free like local-first-v1. State is written to a
file in the workspace so a fresh mock server, started fresh each isolated
step, still sees what a previous step did.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..cloud import credentials_path, vault_request
from ..process import ExecutionError

MOCK_PORT = 8767
STATE_FILE = Path(".outcomeci/mock-cloud-vault.json")


def _state(root: Path) -> dict[str, Any]:
    path = root / STATE_FILE
    if not path.exists():
        raise ExecutionError("mock cloud Vault session was not established")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(root: Path, value: dict[str, Any]) -> None:
    path = root / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def mock_session(root: Path) -> dict[str, Any]:
    """Write a synthetic, already-expired cloud session to force the
    automatic-refresh path on the very next authorized request."""
    state = {
        "entries": {},
        "next_id": 1,
        "valid_access_token": f"access_{os.urandom(8).hex()}",
        "valid_refresh_token": f"refresh_{os.urandom(8).hex()}",
    }
    _write_state(root, state)
    credentials_path().parent.mkdir(parents=True, exist_ok=True)
    credentials_path().write_text(
        json.dumps(
            {
                "api_url": f"http://127.0.0.1:{MOCK_PORT}",
                "access_token": "expired-access-token",
                "refresh_token": state["valid_refresh_token"],
                "credential_type": "session",
            }
        ),
        encoding="utf-8",
    )
    return {"status": "session_established", "workspace_id": "proof_workspace"}


def _mock_cloud_server(root: Path) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def _authorized(self, state: dict[str, Any]) -> bool:
            return self.headers.get("Authorization") == f"Bearer {state['valid_access_token']}"

        def _handle(self, method: str) -> None:
            state = _state(root)
            if self.path == "/v1/auth/refresh" and method == "POST":
                body = self._body()
                if body.get("refresh_token") != state["valid_refresh_token"]:
                    self._send(401, {"detail": "invalid refresh token"})
                    return
                state["valid_access_token"] = f"access_{os.urandom(8).hex()}"
                _write_state(root, state)
                self._send(
                    200,
                    {
                        "access_token": state["valid_access_token"],
                        "refresh_token": state["valid_refresh_token"],
                        "expires_in": 3600,
                    },
                )
                return
            if not self._authorized(state):
                self._send(401, {"detail": "invalid or expired access token"})
                return
            parts = self.path.split("/")
            # /v1/workspaces/{id}/vault[...]
            if self.path.endswith("/vault") and method == "GET":
                self._send(
                    200,
                    {
                        "entries": [
                            {
                                "id": entry_id,
                                "path": entry["path"],
                                "kind": "secret",
                                "provider": None,
                                "status": "active",
                                "workflow_ids": entry["workflow_ids"],
                            }
                            for entry_id, entry in state["entries"].items()
                        ]
                    },
                )
                return
            if self.path.endswith("/vault/secrets") and method == "POST":
                body = self._body()
                entry_id = f"entry_{state['next_id']}"
                state["next_id"] += 1
                state["entries"][entry_id] = {
                    "path": body["path"],
                    "value": body["value"],
                    "workflow_ids": body.get("workflow_ids", []),
                }
                _write_state(root, state)
                self._send(201, {"id": entry_id, "path": body["path"]})
                return
            if (
                "/vault/secrets/" in self.path
                and self.path.endswith("/rotate")
                and method == "POST"
            ):
                entry_id = parts[-2]
                if entry_id not in state["entries"]:
                    self._send(404, {"detail": "unknown entry"})
                    return
                body = self._body()
                state["entries"][entry_id]["value"] = body["value"]
                _write_state(root, state)
                self._send(200, {"id": entry_id, "rotated": True})
                return
            if "/vault/entries/" in self.path and self.path.endswith("/grants") and method == "PUT":
                entry_id = parts[-2]
                if entry_id not in state["entries"]:
                    self._send(404, {"detail": "unknown entry"})
                    return
                body = self._body()
                state["entries"][entry_id]["workflow_ids"] = body.get("workflow_ids", [])
                _write_state(root, state)
                self._send(200, {"id": entry_id, "granted": True})
                return
            self._send(404, {"detail": "unknown route"})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)


def _with_mock_server(root: Path, call: Any) -> Any:
    server = _mock_cloud_server(root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return call()
    finally:
        server.shutdown()
        thread.join(timeout=2)


def vault_put(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    result = _with_mock_server(
        root,
        lambda: vault_request(
            "proof_workspace",
            "put",
            path=request["path"],
            display_name=request.get("display_name", request["path"]),
            value=request["value"],
            workflow_ids=request.get("workflow_ids", []),
        ),
    )
    context.setdefault("cloud_vault", {})[request["path"]] = {"entry_id": result["id"]}
    return {"status": "stored", "entry_id": result["id"]}


def vault_rotate(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    tracked = context.get("cloud_vault", {}).get(request["path"])
    if tracked is None:
        raise ExecutionError(f"cannot rotate {request['path']}: no cloud Vault entry was stored")
    _with_mock_server(
        root,
        lambda: vault_request(
            "proof_workspace", "rotate", entry_id=tracked["entry_id"], value=request["value"]
        ),
    )
    return {"status": "rotated", "entry_id": tracked["entry_id"]}


def vault_verify(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    tracked = context.get("cloud_vault", {}).get(request["path"])
    if tracked is None:
        raise ExecutionError(f"cannot verify {request['path']}: no cloud Vault entry was stored")
    entries = _with_mock_server(root, lambda: vault_request("proof_workspace", "list"))
    entry = next((e for e in entries["entries"] if e["id"] == tracked["entry_id"]), None)
    if entry is None:
        raise ExecutionError(f"cloud Vault entry for {request['path']} was not found")
    state = _state(root)
    current_value = state["entries"][tracked["entry_id"]]["value"]
    return {
        "status": "verified",
        "current_value_matches": current_value == request["expected_value"],
        "workflow_ids": entry["workflow_ids"],
    }
