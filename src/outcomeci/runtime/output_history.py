"""Pinned historical inputs and recoverable projections of authoritative JSON."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from outcomeci.artifacts.output_evidence import read_bytes, safe_path
from outcomeci.artifacts.output_store import MAX_DATABASE_BYTES, OutputStore, OutputStoreError
from outcomeci.runtime.process import ExecutionError
from outcomeci.security import atomic_write_json


def identity(root: Path, config: Path) -> str:
    directory = root / ".outcomeci" / "output-history"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = safe_path(root, ".outcomeci/output-history/identities.sqlite", "output-history")
    key = str(config.resolve().relative_to(root.resolve()))
    with sqlite3.connect(path) as registry:
        path.chmod(0o600)
        registry.execute(
            "CREATE TABLE IF NOT EXISTS workflows (path TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE)"
        )
        registry.execute("INSERT OR IGNORE INTO workflows VALUES (?, ?)", (key, str(uuid.uuid4())))
        return registry.execute("SELECT id FROM workflows WHERE path=?", (key,)).fetchone()[0]


def database(root: Path, workflow_id: str) -> Path:
    key = hashlib.sha256(workflow_id.encode()).hexdigest()
    return safe_path(root, f".outcomeci/output-history/{key}/outputs.sqlite", "output-history")


def _failure(root, compiled, state, exc):
    from outcomeci.runtime.engine import _write

    state.update(status="error", error=str(exc))
    if "output_history" in state:
        publish(root, compiled, state)
    _write(root, state)
    raise ExecutionError(str(exc)) from exc


def prepare(root, config, compiled, state, options):
    try:
        _prepare(root, config, compiled, state, options)
    except (ExecutionError, OutputStoreError, OSError, ValueError, KeyError) as exc:
        _failure(root, compiled, state, exc)


def _prepare(root, config, compiled, state, options):
    if "output_history" not in compiled:
        return
    from outcomeci.runtime.engine import _write

    history = state.get("output_history")
    if history is None:
        if options.output_history_client:
            snapshot = options.output_history_client("pin", {})
            history = {"snapshot": snapshot, "receipts": {}, "managed": True}
        else:
            workflow_id = identity(root, config)
            snapshot_path = (
                database(root, workflow_id).parent / "snapshots" / f"{state['run_id']}.sqlite"
            )
            with OutputStore(
                database(root, workflow_id), workspace_id="local", workflow_id=workflow_id
            ) as store:
                snapshot = store.snapshot(snapshot_path)
            history = {
                "workflow_id": workflow_id,
                "snapshot": snapshot,
                "snapshot_path": str(snapshot_path.relative_to(root)),
                "receipts": {},
            }
        state["output_history"] = history
        _write(root, state)  # Pin before queries or side effects.
    if not history.get("managed"):
        validate_evidence(root, history)
    else:
        for receipt in history["receipts"].values():
            verified = options.output_history_client("query", receipt["request"])
            if verified["records"] != receipt["records"]:
                raise ExecutionError("pinned historical input is no longer available")
    resolve(root, compiled, state, options, None)


def validate_evidence(root, history):
    path = safe_path(root, history["snapshot_path"], "output-history")
    if (
        not path.is_file()
        or hashlib.sha256(read_bytes(path, MAX_DATABASE_BYTES)).hexdigest()
        != history["snapshot"]["digest"]
    ):
        raise ExecutionError("pinned output snapshot is unavailable or corrupt")
    for receipt in history["receipts"].values():
        for record in receipt["records"]:
            artifact = safe_path(root, record["artifact_id"])
            if (
                not artifact.is_file()
                or hashlib.sha256(read_bytes(artifact)).hexdigest() != record["content_hash"]
            ):
                raise ExecutionError("historical source artifact was deleted or changed")


def resolve(root, compiled, state, options, step):
    try:
        _resolve(root, compiled, state, options, step)
    except (ExecutionError, OutputStoreError, OSError, ValueError, KeyError) as exc:
        _failure(root, compiled, state, exc)


def _resolve(root, compiled, state, options, step):
    declaration = compiled.get("output_history")
    if not declaration:
        return
    from outcomeci.runtime import steps
    from outcomeci.runtime.engine import _write

    history = state["output_history"]
    queries = (
        declaration["queries"]
        if step is None
        else compiled["instructions"]["steps"][step]["v1"].get("query", {})
    )
    for name, raw in queries.items():
        key = f"{step or '$workflow'}.{name}"
        if key in history["receipts"]:
            continue
        if len(history["receipts"]) >= 20:
            raise ExecutionError("history query run count budget exceeded")
        request = json.loads(json.dumps(raw))
        source_name = request.pop("source")
        for predicates in request.get("where", {}).values():
            for op, value in predicates.items():
                if isinstance(value, dict):
                    resolved = steps.value(root, state, value["ref"])
                    if resolved is steps.MISSING:
                        raise ExecutionError(f"history query missing input: {value['ref']}")
                    predicates[op] = resolved
        try:
            if history.get("managed"):
                result = options.output_history_client(
                    "query",
                    {
                        "query_name": name,
                        "step_id": step,
                        "source": source_name,
                        "query": request,
                        "run_id": state["run_id"],
                    },
                )
            else:
                path = safe_path(root, history["snapshot_path"], "output-history")
                if (
                    not path.is_file()
                    or hashlib.sha256(read_bytes(path, MAX_DATABASE_BYTES)).hexdigest()
                    != history["snapshot"]["digest"]
                ):
                    raise ExecutionError("pinned output snapshot is unavailable or corrupt")
                with OutputStore(
                    path, workspace_id="local", workflow_id=history["workflow_id"], read_only=True
                ) as store:
                    result = store.query(
                        request,
                        source=declaration["sources"][source_name],
                        consuming_run_id=state["run_id"],
                    )
                for record in result["records"]:
                    artifact = safe_path(root, record["artifact_id"])
                    if (
                        not artifact.is_file()
                        or hashlib.sha256(read_bytes(artifact)).hexdigest()
                        != record["content_hash"]
                    ):
                        raise ExecutionError("historical source artifact was deleted or changed")
            if (
                len(json.dumps(history["receipts"]).encode()) + len(json.dumps(result).encode())
                > 1024 * 1024
            ):
                raise ExecutionError("history query run byte budget exceeded")
            if history.get("managed"):
                result["request"] = {
                    "query_name": name,
                    "step_id": step,
                    "source": source_name,
                    "query": request,
                    "run_id": state["run_id"],
                }
            history["receipts"][key] = result
            _write(root, state)
        except OutputStoreError as exc:
            raise ExecutionError(str(exc)) from exc


def inputs(state):
    history = state.get("output_history", {})
    result = {}
    for key, receipt in history.get("receipts", {}).items():
        owner, name = key.split(".", 1)
        if owner in {"$workflow", state.get("step")}:
            result[name] = [record["values"] for record in receipt["records"]]
    return result


def accept(root, compiled, state, step, count=None):
    """Capture accepted provenance into durable run state before sidecar publication."""
    indexed = compiled.get("output_history", {}).get("indexes", {}).get(step)
    if not indexed:
        return
    history = state["output_history"]
    records = history.setdefault("accepted_records", [])
    if any(r["step_id"] == step for r in records):
        return
    block = compiled["instructions"]["steps"][step]["v1"]
    folder = root / ".outcomeci" / "outcomes" / state["run_id"]
    if "for_each" in block:
        if count is None:
            raise ExecutionError("accepted loop iteration count is missing")
        paths = [folder / block["returns"]["item"]["path"].format(index=i) for i in range(count)]
    else:
        paths = [folder / block["returns"]["path"]]
    accepted_at = datetime.now(UTC).isoformat()
    pending = []
    for path in paths:
        artifact_id = str(path.relative_to(root))
        path = safe_path(root, artifact_id)
        pending.append(
            {
                "run_id": state["run_id"],
                "step_id": step,
                "execution_instance_id": str(path.relative_to(folder)),
                "attempt_id": str(state.get("output_attempts", {}).get(step, 1)),
                "output_name": "returns",
                "workflow_revision": compiled["workflow_revision"],
                "artifact_id": artifact_id,
                "artifact_path": str(path.relative_to(folder)),
                "artifact_version": "1",
                "content_hash": hashlib.sha256(read_bytes(path)).hexdigest(),
                "completed_at": accepted_at,
                "indexed_fields": indexed["fields"],
                "field_types": indexed["field_types"],
            }
        )
    records.extend(pending)


def recover_sidecar(root, state):
    """Recreate a failed sidecar from provenance persisted with accepted completion."""
    records = state.get("output_history", {}).get("accepted_records")
    if records is None:
        return False
    path = safe_path(root, f".outcomeci/outcomes/{state['run_id']}/output-index.json")
    atomic_write_json(path, {"schema_version": 1, "records": records})
    return True


def publish(root, compiled, state, step=None):
    if "output_history" not in compiled:
        return
    try:
        _publish(root, compiled, state, step)
    except (OutputStoreError, ExecutionError, OSError, ValueError) as exc:
        state["output_history"].update(publication_status="failed", publication_error=str(exc))


def _publish(root, compiled, state, step=None):
    """Persist projection evidence first; failure never reruns a producing step."""
    if "output_history" not in compiled:
        return
    history = state["output_history"]
    folder = root / ".outcomeci" / "outcomes" / state["run_id"]
    sidecar = safe_path(root, str((folder / "output-index.json").relative_to(root)))
    evidence = (
        json.loads(read_bytes(sidecar))
        if sidecar.exists()
        else {"schema_version": 1, "records": []}
    )
    if step is not None and not any(
        r["step_id"] == step for r in history.get("accepted_records", [])
    ):
        accept(root, compiled, state, step)
    evidence["records"] = history.get("accepted_records", evidence["records"])
    evidence["run_status"] = (
        "succeeded"
        if state["status"] == "completed"
        else "failed"
        if state["status"] in {"error", "failed"}
        else "cancelled"
        if state["status"] == "cancelled"
        else "running"
    )
    atomic_write_json(sidecar, evidence)
    if history.get("managed"):
        history["publication_status"] = "pending"
        return
    try:
        with OutputStore(
            database(root, history["workflow_id"]),
            workspace_id="local",
            workflow_id=history["workflow_id"],
        ) as store:
            for record in evidence["records"]:
                artifact = safe_path(root, record["artifact_id"])
                if hashlib.sha256(read_bytes(artifact)).hexdigest() != record["content_hash"]:
                    raise ExecutionError("accepted output artifact changed before indexing")
                store.ingest(
                    {
                        **record,
                        "values": json.loads(read_bytes(artifact)),
                        "run_status": evidence["run_status"],
                    }
                )
            store.set_run_status(state["run_id"], evidence["run_status"])
        history["publication_status"] = "ready"
    except (OutputStoreError, ExecutionError, OSError, ValueError) as exc:
        history.update(publication_status="failed", publication_error=str(exc))


def import_history(work: Path, root: Path, config: Path) -> None:
    """Merge container-produced evidence, never replace a shared history database."""
    import shutil

    from outcomeci.artifacts.output_commands import rebuild

    workflow_id = identity(root, config)
    directory = database(root, workflow_id).parent / "snapshots"
    source = database(work, workflow_id).parent / "snapshots"
    if source.is_dir() and not source.is_symlink():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in source.glob("*.sqlite"):
            if path.is_symlink() or not path.is_file():
                continue
            target = directory / path.name
            if not target.exists():
                shutil.copyfile(path, target)
                target.chmod(0o600)
    rebuild(root, config)


def copy_history(source: Path, destination: Path) -> None:
    """Copy live SQLite state using backup transactions, never raw WAL files."""
    directory = source / ".outcomeci" / "output-history"
    if not directory.is_dir():
        return
    for candidate in directory.rglob("*.sqlite"):
        relative = str(candidate.relative_to(source))
        path = safe_path(source, relative, "output-history")
        if path.stat().st_size > MAX_DATABASE_BYTES:
            raise ExecutionError("output-history database exceeds copy limit")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Snapshots are immutable and digest-pinned; backup can change their
        # byte layout, so copy their bounded verified bytes exactly.
        if path.parent.name == "snapshots":
            target.write_bytes(read_bytes(path, MAX_DATABASE_BYTES))
        else:
            with (
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as origin,
                sqlite3.connect(target) as backup,
            ):
                origin.backup(backup)
        target.chmod(0o600)
