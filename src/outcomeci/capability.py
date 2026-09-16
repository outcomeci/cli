"""Run-scoped broker for human hooks across the agent security boundary."""

from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import compile_workflow
from .humans import accept, poll, request, transport_responses
from .integrations import (
    CredentialResolver,
    IntegrationExecutor,
    local_credential_resolver,
)
from .policy import PolicyExecutor
from .process import ExecutionError


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            payload = json.loads(self.rfile.readline(1_000_000))
            result = self.server.dispatch(payload)  # type: ignore[attr-defined]
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())


class Broker:
    def __init__(
        self,
        root: Path,
        config: Path,
        run_id: str,
        phase: str,
        socket_path: Path,
        compiled=None,
        resolver: CredentialResolver | None = None,
        event_sink=None,
    ):
        compiled = compiled if compiled is not None else compile_workflow(config)
        hooks = compiled["instructions"]["phases"][phase]["humans"]
        self.hooks = {
            hook["id"]: hook
            for timing in ("before", "during", "after")
            for hook in hooks[timing]
            if hook.get("delivery", {}).get("type") in {"slack", "custom"}
            and hook.get("delivery", {}).get("targets")
        }
        self.root, self.config, self.run_id, self.phase = root, config, run_id, phase
        run_directory = root / ".outcomeci" / "outcomes" / run_id
        state_path = run_directory / "run.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        self.integrations = PolicyExecutor(
            IntegrationExecutor(
                compiled,
                resolver=resolver or local_credential_resolver(root),
                reviewed=True,
            ),
            root / ".outcomeci" / ".broker" / run_id,
            {
                "intent": state.get("intent"),
                "trigger": state.get("trigger"),
                "phase": compiled["instructions"]["phases"][phase],
            },
            event_sink=event_sink,
        )
        self.token = secrets.token_urlsafe(32)
        self.server = _Server(str(socket_path), _Handler)
        self.server.dispatch = self.dispatch  # type: ignore[attr-defined]

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not secrets.compare_digest(str(payload.get("token", "")), self.token):
            raise ExecutionError("invalid outcome capability")
        if payload.get("run_id") != self.run_id:
            raise ExecutionError("capability is not authorized for this outcome run")
        if payload.get("kind") == "integration":
            inputs = payload.get("inputs")
            if not isinstance(inputs, dict):
                raise ExecutionError("integration input must be an object")
            return self.integrations.execute(
                str(payload.get("capability", "")), inputs, phase=self.phase
            )
        if payload.get("interaction_id") not in self.hooks:
            raise ExecutionError("human hook is not authorized for the current phase")
        operation = payload.get("operation")
        hook = str(payload["interaction_id"])
        if operation == "request":
            return request(
                self.root,
                self.config,
                self.run_id,
                hook,
                bool(payload.get("continue_while_waiting")),
                self.hooks[hook],
            )
        if operation == "poll":
            return poll(
                self.root,
                self.config,
                self.run_id,
                hook,
                int(payload.get("wait_seconds", 0)),
                float(payload.get("interval_seconds", 2)),
            )
        if operation == "accept":
            message = str(payload.get("message", ""))
            matches = list(
                (self.root / ".outcomeci" / "outcomes" / self.run_id / "interactions").glob(
                    f"*/{hook}.json"
                )
            )
            if len(matches) != 1:
                raise ExecutionError(f"interaction {hook} was not found for outcome {self.run_id}")
            interaction = json.loads(matches[0].read_text(encoding="utf-8"))
            replies = transport_responses(self.root, self.config, interaction)
            if not any(reply.get("message") == message for reply in replies):
                raise ExecutionError("response was not verified in the configured Slack thread")
            return accept(
                self.root,
                self.config,
                self.run_id,
                hook,
                message,
                bool(payload.get("approve")),
                bool(payload.get("reject")),
            )
        raise ExecutionError("operation is not allowed by this outcome capability")


@contextmanager
def serve(
    root: Path,
    config: Path,
    run_id: str,
    phase: str,
    *,
    compiled=None,
    resolver: CredentialResolver | None = None,
    event_sink=None,
) -> Iterator[dict[str, str]]:
    temporary = tempfile.TemporaryDirectory(prefix="oci-cap-")
    directory = Path(temporary.name)
    socket_path = directory / "human.sock"
    socket_path.unlink(missing_ok=True)
    broker = Broker(
        root,
        config,
        run_id,
        phase,
        socket_path,
        compiled,
        resolver,
        event_sink,
    )
    thread = threading.Thread(target=broker.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "OUTCOMECI_CAPABILITY_SOCKET": str(socket_path),
            "OUTCOMECI_CAPABILITY_TOKEN": broker.token,
            "OUTCOMECI_RUN_ID": run_id,
        }
    finally:
        broker.server.shutdown()
        broker.server.server_close()
        socket_path.unlink(missing_ok=True)
        temporary.cleanup()


def invoke(operation: str, run_id: str, interaction_id: str, **arguments: Any) -> dict[str, Any]:
    socket_path = os.environ.get("OUTCOMECI_CAPABILITY_SOCKET")
    token = os.environ.get("OUTCOMECI_CAPABILITY_TOKEN")
    if not socket_path or not token:
        raise ExecutionError("no run-scoped human capability is available")
    payload = {
        "token": token,
        "operation": operation,
        "run_id": run_id,
        "interaction_id": interaction_id,
        **arguments,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        response = json.loads(client.makefile("r", encoding="utf-8").readline())
    if not response.get("ok"):
        raise ExecutionError(str(response.get("error", "capability request failed")))
    return response["result"]


def invoke_integration(capability: str, inputs: dict[str, Any]) -> dict[str, Any]:
    run_id = os.environ.get("OUTCOMECI_RUN_ID")
    socket_path = os.environ.get("OUTCOMECI_CAPABILITY_SOCKET")
    token = os.environ.get("OUTCOMECI_CAPABILITY_TOKEN")
    if not run_id or not socket_path or not token:
        raise ExecutionError("no run-scoped integration capability is available")
    payload = {
        "token": token,
        "kind": "integration",
        "run_id": run_id,
        "capability": capability,
        "inputs": inputs,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        response = json.loads(client.makefile("r", encoding="utf-8").readline())
    if not response.get("ok"):
        raise ExecutionError(str(response.get("error", "integration request failed")))
    return response["result"]
