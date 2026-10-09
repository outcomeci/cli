"""Bounded, exact run snapshots for human waits; never captures runner credentials."""

import base64
import hashlib
import json
import re
from pathlib import Path

from outcomeci.cloud_runner.models import ContractError
from outcomeci.security import private_path

MAX_FILES = 500
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _prefix(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ContractError("invalid checkpoint run ID")
    return Path(".outcomeci/outcomes") / run_id


def capture(root: Path, run_id: str) -> list[dict[str, str]]:
    prefix = _prefix(run_id)
    base = root / prefix
    journal = root / ".outcomeci/.broker" / run_id / "journal.json"
    records = []
    if base.is_symlink() or not base.resolve().is_relative_to(root.resolve()) or not base.is_dir():
        raise ContractError("invalid checkpoint directory")
    if journal.is_symlink() or not journal.resolve().is_relative_to(root.resolve()):
        raise ContractError("invalid checkpoint journal")
    for path in base.rglob("*"):
        if path.is_symlink():
            raise ContractError("checkpoint cannot contain symbolic links")
    if journal.exists():
        if journal.stat().st_size > MAX_FILE_BYTES:
            raise ContractError("checkpoint journal is too large")
        target = base / ".resume/journal.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(journal.read_bytes())
    meta = base / ".resume/workspace.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"root": str(root.resolve())}))
    total = 0
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ContractError("checkpoint cannot contain symbolic links")
        if not path.is_file() or private_path(path.relative_to(base)):
            continue
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(records) >= MAX_FILES:
            raise ContractError("checkpoint is too large")
        data = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "content_base64": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    # One validator serves capture and restore; bounds fail rather than silently losing state.
    _validated(records, run_id)
    return records


def _validated(records, run_id):
    prefix = _prefix(run_id)
    if not isinstance(records, list) or not records or len(records) > MAX_FILES:
        raise ContractError("invalid checkpoint file count")
    seen, total, decoded = set(), 0, []
    for record in records:
        if not isinstance(record, dict):
            raise ContractError("invalid checkpoint record")
        name = record.get("path", "")
        if not isinstance(name, str):
            raise ContractError("invalid checkpoint path")
        path = Path(name)
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != name
            or not path.is_relative_to(prefix)
            or private_path(path)
            or name in seen
        ):
            raise ContractError("invalid checkpoint path")
        if {".codex", ".claude", ".ssh"}.intersection(path.parts) or path.name in {
            "auth.json",
            "credentials.json",
        }:
            raise ContractError("private credential file in checkpoint")
        seen.add(name)
        encoded = record.get("content_base64", "")
        if not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES + 2) // 3 * 4:
            raise ContractError("checkpoint file is too large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContractError("invalid checkpoint encoding") from exc
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ContractError("checkpoint is too large")
        if hashlib.sha256(data).hexdigest() != record.get("sha256"):
            raise ContractError("checkpoint digest mismatch")
        decoded.append((path, data))
    if str(prefix / "run.json") not in seen:
        raise ContractError("checkpoint has no run state")
    return decoded


def restore(root: Path, run_id: str, records):
    decoded = _validated(records, run_id)
    for path, data in decoded:
        target = root / path
        if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
            raise ContractError("checkpoint escaped its workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    prefix = _prefix(run_id)
    state = json.loads((root / prefix / "run.json").read_text())
    if state.get("run_id") != run_id or state.get("status") != "waiting":
        raise ContractError("checkpoint is not a suspended run")
    journal = root / prefix / ".resume/journal.json"
    if journal.exists():
        target = root / ".outcomeci/.broker" / run_id / "journal.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(journal.read_bytes())

    metadata = root / prefix / ".resume/workspace.json"
    if metadata.exists():
        previous = Path(json.loads(metadata.read_text())["root"])
        previous_run = previous / prefix
        # Only runtime-owned attachment references are paths; plan strings stay exact.
        for file in (root / prefix).glob("*/consultation.json"):
            consultation = json.loads(file.read_text())
            for turn in consultation.get("turns", []):
                for attachment in turn.get("files", []):
                    path = attachment.get("path")
                    if isinstance(path, str) and Path(path).is_relative_to(previous_run):
                        attachment["path"] = str(
                            root / prefix / Path(path).relative_to(previous_run)
                        )
            file.write_text(json.dumps(consultation))
