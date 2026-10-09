"""Run-scoped broker for API capabilities across the agent security boundary."""

from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from outcomeci.broker.executor import (
    CredentialResolver,
    IntegrationExecutor,
    attachments_path,
    local_credential_resolver,
)
from outcomeci.broker.policy import PolicyExecutor
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import compile_workflow


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
        step: str,
        socket_path: Path,
        compiled=None,
        resolver: CredentialResolver | None = None,
        event_sink=None,
        policy_reviewer=None,
        grants=None,
        container_isolated=False,
        inputs=None,
        connector_client=None,
    ):
        compiled = compiled if compiled is not None else compile_workflow(config)
        self.root, self.config, self.run_id, self.step = root, config, run_id, step
        run_directory = root / ".outcomeci" / "outcomes" / run_id
        state_path = run_directory / "run.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        node = compiled["instructions"]["steps"][step]
        step_policy = None
        if "v1" in node:
            # A v1 step's grants are resolved by the caller before its agent
            # starts; with none given, the step may call nothing.
            grants = grants if grants is not None else []
            if node["v1"].get("policy"):
                step_policy = {
                    "content": (
                        f"Step policy for {step}: {node['v1']['policy']}\n"
                        "Grant arguments such as the channel or repository are enforced "
                        "separately; judge the proposal against this policy only.\n"
                        "context.inputs is everything this step was given, such as a plan "
                        "its requester approved in discussion, and is what it must do.\n"
                        "Where the proposal replaces a file, or several, `compared` is "
                        "the diff against each current copy, with `compared.files` "
                        "listing each file of a write of several: judge the lines it "
                        "changes, not the whole file."
                    ),
                    "policy": node["policy"],
                }
        self.integrations = PolicyExecutor(
            IntegrationExecutor(
                compiled,
                resolver=resolver or local_credential_resolver(root),
                reviewed=True,
                connector_client=connector_client,
                downloads=attachments_path(root, run_id),
            ),
            root / ".outcomeci" / ".broker" / run_id,
            # A v1 step is reviewed against what it was given, which holds the
            # trigger only when the step takes it (`from: trigger`).
            (
                {"inputs": inputs or [], "step": node}
                if "v1" in node
                else {
                    "intent": state.get("intent"),
                    "trigger": state.get("trigger"),
                    **({"inputs": inputs} if inputs is not None else {}),
                    "step": node,
                }
            ),
            reviewer=policy_reviewer,
            event_sink=event_sink,
            grants=grants,
            step_policy=step_policy,
            container_isolated=container_isolated,
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
                str(payload.get("capability", "")), inputs, step=self.step
            )
        raise ExecutionError("operation is not allowed by this outcome capability")


@contextmanager
def serve(
    root: Path,
    config: Path,
    run_id: str,
    step: str,
    *,
    compiled=None,
    resolver: CredentialResolver | None = None,
    event_sink=None,
    policy_reviewer=None,
    grants=None,
    container_isolated=False,
    inputs=None,
    connector_client=None,
) -> Iterator[dict[str, str]]:
    temporary = tempfile.TemporaryDirectory(prefix="oci-cap-")
    directory = Path(temporary.name)
    socket_path = directory / "capability.sock"
    socket_path.unlink(missing_ok=True)
    broker = Broker(
        root,
        config,
        run_id,
        step,
        socket_path,
        compiled,
        resolver,
        event_sink,
        policy_reviewer,
        grants,
        container_isolated,
        inputs,
        connector_client,
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


def _call_broker(
    socket_path: str, payload: dict[str, Any], *, error_fallback: str
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        response = json.loads(client.makefile("r", encoding="utf-8").readline())
    if not response.get("ok"):
        raise ExecutionError(str(response.get("error", error_fallback)))
    return response["result"]


def invoke_integration(
    capability: str, inputs: dict[str, Any], *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Call the run's capability broker, found through `env` (the process
    environment by default, as an agent's CLI sees it)."""
    env = os.environ if env is None else env
    run_id = env.get("OUTCOMECI_RUN_ID")
    socket_path = env.get("OUTCOMECI_CAPABILITY_SOCKET")
    token = env.get("OUTCOMECI_CAPABILITY_TOKEN")
    if not run_id or not socket_path or not token:
        raise ExecutionError("no run-scoped integration capability is available")
    payload = {
        "token": token,
        "kind": "integration",
        "run_id": run_id,
        "capability": capability,
        "inputs": inputs,
    }
    return _call_broker(socket_path, payload, error_fallback="integration request failed")
