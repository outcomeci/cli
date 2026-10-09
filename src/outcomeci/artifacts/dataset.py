"""Turn run directories into a dataset, and check that each run is complete.

A run directory is the unit: its trigger, each step's outputs, every call
with its result, every policy decision, the model turns, and the outcome. The
export walks run directories, in a workspace's storage or under a local
repository's `.outcomeci/outcomes`, reports what each run has and lacks, and
writes one JSON line per run. The check alone needs only the listing, so it is
quick enough to run after every change to what the runner records.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from outcomeci import cloud

ROW_SCHEMA = "outcomeci.dataset.run/v1"
# Files every finished run carries at its root.
ROOT_RECORDS = ("run.json", "manifest.json", "effects.json", "calls.json", "policy.json")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".yml", ".yaml", ".csv"}
# A model turn the api captured: under model-turns/<step>/, or, from an api
# that listed captures by object id, as <object id>/model-turns_<step>_<id>.json.
MODEL_TURN = re.compile(
    r"^(?:model-turns/(?P<step>[^/]+)/|[^/]+/model-turns_(?P<legacy>.+?)_[0-9a-f-]{36}\.json$)"
)


def model_turn_step(path: str) -> str | None:
    """The step a captured model turn belongs to, or None for other files."""
    match = MODEL_TURN.match(path)
    if not match:
        return None
    return match.group("step") or match.group("legacy")


def _steps(run_state: dict[str, Any] | None) -> list[str]:
    if not run_state:
        return []
    steps = run_state.get("completed_steps")
    if isinstance(steps, list):
        return [step for step in steps if isinstance(step, str)]
    return []


def completeness(paths: Iterable[str], run_state: dict[str, Any] | None) -> dict[str, Any]:
    """What a run directory holds against what a finished run should hold.

    `paths` are the run's file paths relative to its directory. A step's
    outputs and its reasoning evidence (a transcript or model turns) are
    expected for every completed step named in run.json.
    """
    names = set(paths)
    present: list[str] = []
    missing: list[str] = []
    for record in ROOT_RECORDS:
        (present if record in names else missing).append(record)
    for step in _steps(run_state):
        outputs = f"{step}/outputs.json"
        (present if outputs in names else missing).append(outputs)
        reasoning = f"reasoning:{step}"
        has_reasoning = any(
            name.startswith(f"transcripts/{step}/") or model_turn_step(name) == step
            for name in names
        )
        (present if has_reasoning else missing).append(reasoning)
    model_turns = sum(1 for name in names if model_turn_step(name) is not None)
    return {
        "complete": not missing,
        "present": present,
        "missing": missing,
        "model_turns": model_turns,
        "files": len(names),
    }


def _decode(path: str, content: bytes) -> Any:
    """A file's value for the row: JSON as data, JSON lines as a list, text as
    text, and anything else as a digest, so a binary attachment never bloats
    a row."""
    suffix = Path(path).suffix.lower()
    if suffix == ".jsonl":
        lines = []
        for line in content.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                lines.append({"raw": line})
        return lines
    if suffix == ".json":
        try:
            return json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    if suffix in TEXT_SUFFIXES:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return {
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "base64": base64.b64encode(content).decode() if len(content) <= 4096 else None,
    }


def run_row(
    run_id: str,
    files: dict[str, bytes],
    *,
    workflow: dict[str, Any] | None = None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One dataset row: the run directory's files, decoded and grouped."""
    decoded = {path: _decode(path, content) for path, content in sorted(files.items())}
    run_state = decoded.get("run.json") if isinstance(decoded.get("run.json"), dict) else None
    steps: dict[str, dict[str, Any]] = {}
    for path, value in decoded.items():
        parts = path.split("/")
        if model_turn_step(path) is not None:
            continue
        if len(parts) >= 2 and parts[0] not in {"transcripts", "model-turns", "attachments"}:
            if parts[-1] == "outputs.json" and len(parts) == 2:
                steps.setdefault(parts[0], {})["outputs"] = value
            elif len(parts) == 3 and parts[1] == "items":
                steps.setdefault(parts[0], {}).setdefault("items", []).append(
                    {"path": path, "value": value}
                )
            elif parts[-1] == "consultation.json" and len(parts) == 2:
                steps.setdefault(parts[0], {})["consultation"] = value
            elif parts[1] == "decisions":
                steps.setdefault(parts[0], {}).setdefault("decisions", []).append(value)
    transcripts: dict[str, dict[str, Any]] = {}
    model_turns: list[Any] = []
    attachments: list[dict[str, Any]] = []
    for path, value in decoded.items():
        parts = path.split("/")
        if parts[0] == "transcripts" and len(parts) >= 3:
            transcripts.setdefault(parts[1], {})["/".join(parts[2:])] = value
        elif model_turn_step(path) is not None:
            model_turns.append(value)
        elif parts[0] == "attachments":
            attachments.append({"path": path, "value": value})
    return {
        "schema_version": ROW_SCHEMA,
        "run_id": run_id,
        "workflow": workflow,
        "run": run,
        "trigger": run_state.get("trigger") if run_state else None,
        "status": run_state.get("status") if run_state else None,
        "workflow_revision": run_state.get("workflow_revision") if run_state else None,
        "usage_records": run_state.get("usage_records") if run_state else None,
        "steps": steps,
        "calls": decoded.get("calls.json"),
        "policy": decoded.get("policy.json"),
        "effects": decoded.get("effects.json"),
        "transcripts": transcripts,
        "model_turns": model_turns,
        "attachments": attachments,
        "manifest": decoded.get("manifest.json"),
        "completeness": completeness(decoded, run_state),
    }


# --- local run directories -------------------------------------------------


def local_runs(root: Path) -> dict[str, dict[str, bytes]]:
    """Every run directory under the repository's `.outcomeci/outcomes`."""
    outcomes = root / ".outcomeci" / "outcomes"
    runs: dict[str, dict[str, bytes]] = {}
    if not outcomes.is_dir():
        return runs
    for directory in sorted(outcomes.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        files = {
            str(path.relative_to(directory)).replace("\\", "/"): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(directory).parts)
        }
        runs[directory.name] = files
    return runs


# --- a workspace's storage -------------------------------------------------


def _walk(workspace_id: str, path: str) -> list[dict[str, Any]]:
    """Every file under a storage folder, recursively, with pagination."""
    files: list[dict[str, Any]] = []
    folders = [path]
    while folders:
        folder = folders.pop()
        cursor: str | None = None
        while True:
            page = cloud.storage_directory(workspace_id, folder, cursor)
            files.extend(page.get("files") or [])
            folders.extend(item["path"] for item in page.get("folders") or [])
            cursor = page.get("cursor")
            if not cursor:
                break
    return files


def _workspace_runs(workspace_id: str, workflow_id: str | None) -> dict[str, dict[str, Any]]:
    """Run id -> the run summary and its workflow, for the runs the api lists."""
    index: dict[str, dict[str, Any]] = {}
    for workflow in cloud.list_workflows(workspace_id):
        if workflow_id and workflow.get("workflow_id") != workflow_id:
            continue
        if not workflow.get("revision"):
            continue
        for run in cloud.list_workflow_runs(workspace_id, workflow["workflow_id"]):
            index[run["run_id"]] = {
                "workflow": {
                    "workflow_id": workflow["workflow_id"],
                    "name": workflow.get("name"),
                },
                "run": run,
            }
    return index


def _relative(path: str, run_folder: str) -> str:
    return path[len(run_folder) :] if path.startswith(run_folder) else path


def workspace_export(
    workspace_id: str,
    *,
    workflow_id: str | None = None,
    check_only: bool = False,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Check, and unless `check_only` export, every run directory in a
    workspace's storage. Returns the report; rows go to `emit`."""
    index = _workspace_runs(workspace_id, workflow_id)
    runs_root = cloud.storage_directory(workspace_id, "runs/")
    folders = list(runs_root.get("folders") or [])
    cursor = runs_root.get("cursor")
    while cursor:
        page = cloud.storage_directory(workspace_id, "runs/", cursor)
        folders.extend(page.get("folders") or [])
        cursor = page.get("cursor")
    report: dict[str, Any] = {
        "workspace_id": workspace_id,
        "runs": [],
        "complete": 0,
        "incomplete": 0,
    }
    for folder in folders:
        run_id = folder["name"]
        meta = index.get(run_id)
        if workflow_id and (meta is None or meta["workflow"]["workflow_id"] != workflow_id):
            continue
        listed = _walk(workspace_id, folder["path"])
        paths = {_relative(item["path"], folder["path"]): item for item in listed}
        if check_only:
            run_state = None
            entry = paths.get("run.json")
            if entry is not None:
                run_state = _decode("run.json", cloud.download_object(workspace_id, entry))
                run_state = run_state if isinstance(run_state, dict) else None
            status = completeness(paths, run_state)
            row = None
        else:
            files = {
                path: cloud.download_object(workspace_id, item) for path, item in paths.items()
            }
            row = run_row(
                run_id,
                files,
                workflow=meta["workflow"] if meta else None,
                run=meta["run"] if meta else None,
            )
            status = row["completeness"]
            if emit:
                emit(row)
        report["runs"].append(
            {
                "run_id": run_id,
                "workflow": meta["workflow"]["name"] if meta else None,
                "status": (meta["run"].get("status") if meta else None),
                **status,
            }
        )
        report["complete" if status["complete"] else "incomplete"] += 1
    return report


def local_export(
    root: Path, *, check_only: bool = False, emit: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    report: dict[str, Any] = {"dir": str(root), "runs": [], "complete": 0, "incomplete": 0}
    for run_id, files in local_runs(root).items():
        row = run_row(run_id, files)
        status = row["completeness"]
        if emit and not check_only:
            emit(row)
        report["runs"].append({"run_id": run_id, "status": row["status"], **status})
        report["complete" if status["complete"] else "incomplete"] += 1
    return report
