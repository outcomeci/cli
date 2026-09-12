"""Run-scoped broker for human hooks across the agent security boundary."""
from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import compile_workflow
from .humans import accept, poll, request
from .slack import poll_replies
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
    def __init__(self, root: Path, config: Path, run_id: str, phase: str, socket_path: Path):
        compiled = compile_workflow(config)
        hooks = compiled["instructions"]["phases"][phase]["humans"]
        self.hooks = {
            hook["id"]: hook for timing in ("before", "during", "after") for hook in hooks[timing]
            if hook.get("delivery", {}).get("type") == "slack" and hook.get("delivery", {}).get("targets")
        }
        self.root, self.config, self.run_id = root, config, run_id
        self.token = secrets.token_urlsafe(32)
        self.server = _Server(str(socket_path), _Handler)
        self.server.dispatch = self.dispatch  # type: ignore[attr-defined]

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not secrets.compare_digest(str(payload.get("token", "")), self.token):
            raise ExecutionError("invalid outcome capability")
        if payload.get("run_id") != self.run_id or payload.get("interaction_id") not in self.hooks:
            raise ExecutionError("human hook is not authorized for the current phase")
        operation = payload.get("operation")
        hook = str(payload["interaction_id"])
        if operation == "request":
            return request(self.root, self.config, self.run_id, hook, bool(payload.get("continue_while_waiting")), self.hooks[hook])
        if operation == "poll":
            return poll(self.root, self.run_id, hook, int(payload.get("wait_seconds", 0)), float(payload.get("interval_seconds", 2)))
        if operation == "accept":
            message = str(payload.get("message", ""))
            replies = poll_replies(self.root, self.run_id, hook)
            if not any(reply.get("message") == message for reply in replies):
                raise ExecutionError("response was not verified in the configured Slack thread")
            return accept(self.root, self.config, self.run_id, hook, message, bool(payload.get("approve")), bool(payload.get("reject")))
        raise ExecutionError("operation is not allowed by this outcome capability")


@contextmanager
def serve(root: Path, config: Path, run_id: str, phase: str) -> Iterator[dict[str, str]]:
    directory = root / ".outcomeci" / "outcomes" / run_id / ".capability"
    directory.mkdir(parents=True, exist_ok=True)
    socket_path = directory / "human.sock"
    socket_path.unlink(missing_ok=True)
    broker = Broker(root, config, run_id, phase, socket_path)
    thread = threading.Thread(target=broker.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"OUTCOMECI_CAPABILITY_SOCKET": str(socket_path), "OUTCOMECI_CAPABILITY_TOKEN": broker.token, "OUTCOMECI_RUN_ID": run_id}
    finally:
        broker.server.shutdown()
        broker.server.server_close()
        socket_path.unlink(missing_ok=True)


def invoke(operation: str, run_id: str, interaction_id: str, **arguments: Any) -> dict[str, Any]:
    socket_path = os.environ.get("OUTCOMECI_CAPABILITY_SOCKET")
    token = os.environ.get("OUTCOMECI_CAPABILITY_TOKEN")
    if not socket_path or not token:
        raise ExecutionError("no run-scoped human capability is available")
    payload = {"token": token, "operation": operation, "run_id": run_id, "interaction_id": interaction_id, **arguments}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        response = json.loads(client.makefile("r", encoding="utf-8").readline())
    if not response.get("ok"):
        raise ExecutionError(str(response.get("error", "capability request failed")))
    return response["result"]
