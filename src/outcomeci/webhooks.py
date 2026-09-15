"""Outbound, credential-private local delivery with durable execution receipts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .process import ExecutionError


class ListenerUnavailable(ExecutionError):
    """Transient transport failure; safe to retry registration/claim only."""


def validate_delivery_config(value: dict[str, Any]) -> dict[str, Any]:
    from .config import ConfigError

    if set(value) - {"type", "delivery"} or value.get("delivery", "queued") != "queued":
        raise ConfigError("Webhooks support asynchronous queued delivery only")
    return {"type": "webhook.received", "delivery": "queued"}


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    Path(temporary).replace(path)


class Listener:
    def __init__(
        self,
        workspace_id: str,
        workflow_id: str,
        root: Path,
        config: Path,
        *,
        auto_continue: bool = False,
    ):
        from .config import compile_workflow

        self.workspace_id, self.workflow_id, self.root, self.config = (
            workspace_id,
            workflow_id,
            root,
            config,
        )
        self.compiled = compile_workflow(config)
        if self.compiled["workflow"]["spec"]["backend"].get("provider") != "filesystem":
            raise ExecutionError("Local listener requires filesystem workflow backend")
        self.content_sha256 = hashlib.sha256(config.read_bytes()).hexdigest()
        self.support_sha256 = self._support_hash()
        self.auto_continue = auto_continue
        self.connector_id = str(uuid4())
        self.prefix = f"/workspaces/{workspace_id}/workflows/{workflow_id}/local-listener"
        self.mutex = threading.RLock()

    def _support_hash(self) -> str:
        from .security import private_path

        support = self.config.parent / ".outcomeci"
        files = {}
        if support.is_dir():
            for path in sorted(support.rglob("*")):
                relative = path.relative_to(support)
                if (
                    path.is_file()
                    and "outcomes" not in relative.parts
                    and not private_path(relative)
                ):
                    if not path.resolve().is_relative_to(
                        self.config.parent.resolve()
                    ) or private_path(path.resolve().relative_to(self.config.parent.resolve())):
                        raise ExecutionError("Support file references a private path")
                    files[str(Path(".outcomeci") / relative)] = base64.b64encode(
                        path.read_bytes()
                    ).decode()
        return hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def request(self, operation: str, body: dict[str, Any]) -> Any:
        from .cloud import _authorized_request

        with self.mutex:
            try:
                status, result = _authorized_request(
                    self.prefix + ("/" + operation if operation else ""), method="POST", body=body
                )
            except ExecutionError as exc:
                if isinstance(exc.__cause__, OSError):
                    raise ListenerUnavailable(
                        "Local listener API is temporarily unavailable"
                    ) from exc
                raise
        if status >= 500 or status == 429:
            raise ListenerUnavailable("Local listener API is temporarily unavailable")
        if status != 200:
            raise ExecutionError(
                f"Local listener {operation or 'registration'} failed (HTTP {status})"
            )
        return result

    def register(self) -> Any:
        return self.request(
            "",
            {
                "connector_id": self.connector_id,
                "content_sha256": self.content_sha256,
                "support_sha256": self.support_sha256,
            },
        )

    def claim(self) -> Any:
        if (
            hashlib.sha256(self.config.read_bytes()).hexdigest() != self.content_sha256
            or self._support_hash() != self.support_sha256
        ):
            raise ExecutionError("Local workflow changed; sync it and restart the listener")
        return self.request("claim", {"connector_id": self.connector_id})

    def execute(self, claim: dict[str, Any]) -> dict[str, Any]:
        from . import local
        from .contracts import ContractError, validate_trigger_payload

        if (
            hashlib.sha256(self.config.read_bytes()).hexdigest() != self.content_sha256
            or self._support_hash() != self.support_sha256
        ):
            raise ExecutionError("Local definition changed before execution")

        payload = claim["input"]
        try:
            validate_trigger_payload(claim["trigger_type"], payload)
        except ContractError:
            # Invalid durable input must become an inspectable failure, not an
            # endless unstarted-lease retry. No agent effects have started.
            with suppress(ExecutionError):
                self.request(
                    "complete",
                    {
                        "connector_id": self.connector_id,
                        "invocation_id": claim["invocation_id"],
                        "lease_token": claim["lease_token"],
                        "status": "failed",
                    },
                )
            raise
        if (
            claim["trigger_name"] not in self.compiled["triggers"]
            or self.compiled["triggers"][claim["trigger_name"]]["type"] != claim["trigger_type"]
        ):
            raise ExecutionError("Claim does not match local async trigger")
        receipt = self.root / ".outcomeci/.broker/listener" / f"{claim['invocation_id']}.json"
        if receipt.exists():
            raise ExecutionError(
                "This invocation has a private local execution receipt; automatic replay refused"
            )
        lease = {
            "connector_id": self.connector_id,
            "invocation_id": claim["invocation_id"],
            "lease_token": claim["lease_token"],
        }
        _save(receipt, {"state": "starting", "invocation_id": claim["invocation_id"]})
        self.request("start", lease)
        _save(receipt, {"state": "running", "invocation_id": claim["invocation_id"]})
        stop = threading.Event()
        failure = []

        def heartbeat() -> None:
            while not stop.wait(10):
                try:
                    self.request("heartbeat", lease)
                except ExecutionError as exc:
                    failure.append(exc)
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        result = None
        try:
            result = local.trigger(self.root, self.config, claim["trigger_name"], payload)
            while True:
                if failure:
                    raise ExecutionError(
                        "Local execution lease was lost; inspect the run before retrying"
                    )
                if len(result.get("completed_phases", [])) == len(
                    self.compiled["instructions"]["phases"]
                ):
                    break
                if result.get("status") == "error":
                    raise ExecutionError("Local workflow recorded an error")
                if (
                    result.get("ready_phases")
                    and self.auto_continue
                    and result.get("status") != "awaiting_input"
                ):
                    result = local.continue_run(
                        self.root, self.config, result["run_id"], approve=True
                    )
                else:
                    # Explicit confirmations/hooks remain authoritative. Users can
                    # continue the durable local run from another CLI/session.
                    time.sleep(1)
                    result = local.status(self.root, result["run_id"])
            self.request(
                "complete", {**lease, "status": "completed", "local_run_id": result["run_id"]}
            )
            _save(receipt, {"state": "completed", "local_run_id": result["run_id"]})
            return result
        except Exception:
            _save(
                receipt,
                {"state": "uncertain", "local_run_id": result.get("run_id") if result else None},
            )
            with suppress(ExecutionError):
                self.request(
                    "complete",
                    {
                        **lease,
                        "status": "failed",
                        "local_run_id": result.get("run_id") if result else None,
                    },
                )
            raise
        finally:
            stop.set()
            thread.join(timeout=1)


def listen(
    workspace_id: str,
    workflow_id: str,
    root: Path,
    config: Path,
    *,
    auto_continue: bool = False,
    once: bool = False,
) -> None:
    listener = Listener(
        workspace_id,
        workflow_id,
        root,
        config,
        auto_continue=auto_continue,
    )
    while True:
        try:
            listener.register()
            break
        except ListenerUnavailable:
            print("API unavailable; retrying connection. Events remain queued.", flush=True)
            time.sleep(5)
    print("Local listener connected. Waiting for version-matched async events.", flush=True)
    while True:
        try:
            claim = listener.claim()
        except ListenerUnavailable:
            print("API unavailable; retrying. Events remain queued.", flush=True)
            time.sleep(5)
            continue
        if claim is None:
            time.sleep(1)
            continue
        print("Event claimed; starting local workflow.", flush=True)
        try:
            listener.execute(claim)
        except Exception:
            print(
                "Local workflow failed or became uncertain; inspect its run before retrying.",
                flush=True,
            )
            if once:
                raise
        else:
            print("Local workflow completed.", flush=True)
        if once:
            return
