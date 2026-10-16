"""Local output-history commands, using the same grants as workflow execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from outcomeci.artifacts.output_evidence import read_bytes as _bytes
from outcomeci.artifacts.output_evidence import safe_path as _artifact
from outcomeci.artifacts.output_store import OutputStore, OutputStoreError
from outcomeci.runtime import output_history
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import compile_workflow


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(_bytes(path))
    if not isinstance(value, dict):
        raise ExecutionError("output-history evidence must be a JSON object")
    return value


def query(root: Path, config: Path, source: str, request: dict[str, Any]) -> dict[str, Any]:
    """Query an existing local index, enforcing current declared source access."""
    try:
        compiled = compile_workflow(config)
        sources = compiled.get("output_history", {}).get("sources", {})
        if source not in sources:
            raise ExecutionError("query must name a declared workflow output source")
        workflow_id = output_history.identity(root, config)
        path = output_history.database(root, workflow_id)
        if not path.is_file():
            raise ExecutionError("output index is unavailable; run oci outputs rebuild first")
        with OutputStore(
            path, workspace_id="local", workflow_id=workflow_id, read_only=True
        ) as store:
            result = store.query(request, source=sources[source])
        for record in result["records"]:
            artifact = _artifact(root, record["artifact_id"])
            if (
                not artifact.is_file()
                or hashlib.sha256(_bytes(artifact)).hexdigest() != record["content_hash"]
            ):
                raise ExecutionError(
                    "historical artifact was deleted or changed; rebuild the index"
                )
        return result
    except (OutputStoreError, OSError, ValueError, KeyError) as exc:
        raise ExecutionError(f"cannot query output history: {exc}") from exc


def rebuild(root: Path, config: Path) -> dict[str, Any]:
    """Replay retained evidence idempotently without invoking producing steps."""
    try:
        compile_workflow(config)
        workflow_id = output_history.identity(root, config)
        indexed = 0
        skipped: list[str] = []
        with OutputStore(
            output_history.database(root, workflow_id),
            workspace_id="local",
            workflow_id=workflow_id,
        ) as store:
            # Invalidate deleted evidence before replay. Never drop unrelated
            # concurrent rows by replacing the whole live database file.
            for record in store.artifact_references():
                artifact = _artifact(root, record["artifact_id"])
                if (
                    not artifact.is_file()
                    or hashlib.sha256(_bytes(artifact)).hexdigest() != record["content_hash"]
                ):
                    store.delete_artifact(record["artifact_id"])
            for state_path in sorted((root / ".outcomeci" / "outcomes").glob("*/run.json")):
                state_path = _artifact(root, str(state_path.relative_to(root)))
                state = _read(state_path)
                history = state.get("output_history", {})
                if history.get("managed") or history.get("workflow_id") != workflow_id:
                    continue
                sidecar = state_path.parent / "output-index.json"
                if not sidecar.is_file() and not output_history.recover_sidecar(root, state):
                    skipped.append(state_path.parent.name)
                    continue
                sidecar = _artifact(root, str(sidecar.relative_to(root)))
                evidence = _read(sidecar)
                if evidence.get("schema_version") != 1 or not isinstance(
                    evidence.get("records"), list
                ):
                    raise ExecutionError("unsupported output-index evidence schema")
                status = (
                    "succeeded"
                    if state.get("status") == "completed"
                    else "failed"
                    if state.get("status") in {"error", "failed", "cancelled"}
                    else "running"
                )
                for record in evidence["records"]:
                    if not isinstance(record, dict):
                        raise ExecutionError("output-index record must be an object")
                    if record.get("run_id") != state.get("run_id"):
                        raise ExecutionError("output-index evidence belongs to a different run")
                    artifact = _artifact(root, record["artifact_id"])
                    if not artifact.is_relative_to(state_path.parent.resolve()):
                        raise ExecutionError("output-index artifact belongs to a different run")
                    if not artifact.is_file():
                        skipped.append(record["artifact_id"])
                        continue
                    values = _read(artifact)
                    if hashlib.sha256(_bytes(artifact)).hexdigest() != record["content_hash"]:
                        skipped.append(record["artifact_id"])
                        continue
                    indexed += int(store.ingest({**record, "values": values, "run_status": status}))
                store.set_run_status(state["run_id"], status)
        return {
            "workflow_id": workflow_id,
            "indexed": indexed,
            "skipped": skipped,
            "complete": not skipped,
        }
    except (OutputStoreError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ExecutionError(f"cannot rebuild output history: {exc}") from exc
