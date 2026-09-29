"""Agent session transcripts: the bytes a run's agent wrote, and their usage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sessions(agent: str) -> list[Path]:
    if agent == "codex":
        configured = os.environ.get("CODEX_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
        found = root.glob("sessions/**/*.jsonl") if root.is_dir() else []
    elif agent == "claude":
        root = Path(os.environ.get("HOME", "")) / ".claude" / "projects"
        found = root.glob("**/*.jsonl") if root.is_dir() else []
    else:
        root = Path(os.environ.get("HOME", "")) / ".local" / "share" / "opencode"
        found = root.glob("**/*.jsonl") if root.is_dir() else []
    return sorted(
        path for path in found if path.is_file() and path.stat().st_size <= 50 * 1024 * 1024
    )


def _usage(path: Path, provider: str) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                usage = item.get("usage")
                info = item.get("info")
                if item.get("type") == "token_count" and isinstance(info, dict):
                    usage = info.get("last_token_usage")
                if isinstance(usage, dict) and any(
                    key in usage
                    for key in ("input_tokens", "output_tokens", "inputTokens", "outputTokens")
                ):
                    records.append(
                        {
                            "provider": provider,
                            "source_line": line_number,
                            "occurred_at": item.get("timestamp") or value.get("timestamp"),
                            "input_tokens": int(
                                usage.get("input_tokens") or usage.get("inputTokens") or 0
                            ),
                            "output_tokens": int(
                                usage.get("output_tokens") or usage.get("outputTokens") or 0
                            ),
                            "cache_read_tokens": int(
                                usage.get("cache_read_input_tokens")
                                or usage.get("cached_input_tokens")
                                or 0
                            ),
                            "cache_write_tokens": int(
                                usage.get("cache_creation_input_tokens")
                                or usage.get("cache_write_input_tokens")
                                or 0
                            ),
                        }
                    )
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return list({json.dumps(item, sort_keys=True): item for item in records}.values())


def _session_details(path: Path, agent: str) -> tuple[str | None, str | None]:
    """Return the session id and cwd without retaining conversation content."""
    found_id: str | None = None
    found_cwd: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for _, line in zip(range(100), source, strict=False):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
                session_id = value.get("sessionId") or payload.get("id")
                cwd = value.get("cwd") or payload.get("cwd")
                if session_id and not found_id:
                    found_id = str(session_id)
                if cwd and not found_cwd:
                    found_cwd = str(cwd)
                if found_id and found_cwd:
                    break
    except OSError:
        pass
    return found_id, found_cwd


def _select_sessions(
    agent: str, session_id: str | None, workspace: Path | None, since: str | None
) -> list[Path]:
    candidates = _sessions(agent)
    if session_id:
        exact = [
            path
            for path in candidates
            if session_id in path.name or _session_details(path, agent)[0] == session_id
        ]
        if exact:
            return exact[:1]
    expected = str(workspace.resolve()) if workspace else None
    scoped = [
        path for path in candidates if expected and _session_details(path, agent)[1] == expected
    ]
    if since:
        try:
            threshold = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
            scoped = [path for path in scoped if path.stat().st_mtime >= threshold]
        except ValueError:
            pass
    return sorted(scoped, key=lambda path: path.stat().st_mtime, reverse=True)[:1]


def _transcripts(
    agent: str,
    root: Path,
    phase: str,
    *,
    session_id: str | None = None,
    byte_offset: int = 0,
    workspace: Path | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    target = root / "transcripts" / phase / agent
    target.mkdir(parents=True, exist_ok=True)
    files, usage, total = [], [], 0
    sources = (
        _select_sessions(agent, session_id, workspace, since)
        if (session_id or workspace)
        else _sessions(agent)
    )
    for index, source in enumerate(sources, 1):
        source_size = source.stat().st_size
        offset = min(byte_offset, source_size) if index == 1 else 0
        size = source_size - offset
        if total + size > 100 * 1024 * 1024:
            break
        destination = target / f"{index:02d}-{source.name}"
        with source.open("rb") as input_file, destination.open("wb") as output_file:
            input_file.seek(offset)
            shutil.copyfileobj(input_file, output_file)
        total += size
        relative = str(destination.relative_to(root))
        records = [{**item, "transcript_path": relative} for item in _usage(destination, agent)]
        usage.extend(records)
        files.append(
            {
                "path": relative,
                "byte_size": size,
                "source_offset": offset,
                "sha256": _sha(destination),
                "usage_records": len(records),
            }
        )
    usage_path = root / "transcripts" / phase / "usage.json"
    usage_path.write_text(
        json.dumps(
            {"schema_version": 1, "provider": agent, "phase": phase, "records": usage},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "provider": agent,
        "phase": phase,
        "files": files,
        "usage_path": str(usage_path.relative_to(root)),
        "usage_records": len(usage),
        "usage": usage[:20000],
    }
