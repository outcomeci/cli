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


# Version 2 records name their model and count input the same way for every
# agent: input_tokens excludes cached tokens, which are cache_read_tokens and
# cache_write_tokens.
USAGE_SCHEMA_VERSION = 2


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _record(
    provider: str,
    model: str | None,
    line_number: int,
    occurred_at: Any,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "source_line": line_number,
        "occurred_at": occurred_at,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


def _usage(path: Path, provider: str, model: str | None = None) -> list[dict[str, Any]]:
    """One usage record per model turn in an agent's session file.

    Claude Code writes a line per content block of a response, each carrying
    the response's usage, so a response counts once, by its message id. Codex
    reports each turn's usage in a token_count event that it sometimes repeats;
    a repeat leaves its running total unchanged and is skipped. Codex counts
    cached tokens inside input_tokens, so they are taken out. OpenCode reports
    each turn on a step-finish part. `model` is the step's configured model,
    used when the session does not name one.
    """
    records: list[dict[str, Any]] = []
    # Newer Codex writes one token_usage_record per model response; when a
    # session has them they replace its token_count events.
    responses: dict[str, dict[str, Any]] = {}
    turn_models: dict[str, str] = {}
    claude: dict[str, int] = {}
    codex_model: str | None = None
    codex_total: Any = None
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
        if value.get("type") == "turn_context" and payload.get("model"):
            codex_model = str(payload["model"])
            if payload.get("turn_id"):
                turn_models[str(payload["turn_id"])] = codex_model
            continue
        if value.get("type") == "token_usage_record":
            usage = payload.get("usage")
            key = payload.get("response_id") or f"line-{line_number}"
            if isinstance(usage, dict) and key not in responses:
                cached = _int(usage.get("cached_input_tokens"))
                responses[key] = _record(
                    provider,
                    turn_models.get(str(payload.get("turn_id"))) or codex_model or model,
                    line_number,
                    value.get("timestamp"),
                    input_tokens=max(_int(usage.get("input_tokens")) - cached, 0),
                    output_tokens=_int(usage.get("output_tokens")),
                    cache_read_tokens=cached,
                    cache_write_tokens=_int(usage.get("cache_write_input_tokens")),
                )
            continue
        if payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            usage = info.get("last_token_usage")
            total = info.get("total_token_usage")
            if not isinstance(usage, dict) or (total is not None and total == codex_total):
                continue
            codex_total = total
            cached = _int(usage.get("cached_input_tokens"))
            records.append(
                {
                    **_record(
                        provider,
                        codex_model or model,
                        line_number,
                        value.get("timestamp"),
                        input_tokens=max(_int(usage.get("input_tokens")) - cached, 0),
                        output_tokens=_int(usage.get("output_tokens")),
                        cache_read_tokens=cached,
                        cache_write_tokens=_int(usage.get("cache_write_input_tokens")),
                    ),
                    "_codex_token_count": True,
                }
            )
            continue
        message = value.get("message") if isinstance(value.get("message"), dict) else {}
        usage = message.get("usage")
        if isinstance(usage, dict) and ("input_tokens" in usage or "output_tokens" in usage):
            record = _record(
                provider,
                message.get("model") or model,
                line_number,
                value.get("timestamp"),
                input_tokens=_int(usage.get("input_tokens")),
                output_tokens=_int(usage.get("output_tokens")),
                cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
                cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
            )
            key = message.get("id")
            if key and key in claude:
                # A later line of the same response: its usage is the latest.
                records[claude[key]] = {
                    **record,
                    "source_line": records[claude[key]]["source_line"],
                }
            else:
                if key:
                    claude[key] = len(records)
                records.append(record)
            continue
        part = value.get("part") if isinstance(value.get("part"), dict) else {}
        tokens = part.get("tokens")
        if part.get("type") == "step-finish" and isinstance(tokens, dict):
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            records.append(
                _record(
                    provider,
                    model,
                    line_number,
                    value.get("timestamp"),
                    input_tokens=_int(tokens.get("input")),
                    output_tokens=_int(tokens.get("output")) + _int(tokens.get("reasoning")),
                    cache_read_tokens=_int(cache.get("read")),
                    cache_write_tokens=_int(cache.get("write")),
                )
            )
    if responses:
        records = [record for record in records if not record.get("_codex_token_count")]
        records.extend(responses.values())
    for record in records:
        record.pop("_codex_token_count", None)
    return records


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
    step: str,
    *,
    session_id: str | None = None,
    byte_offset: int = 0,
    workspace: Path | None = None,
    since: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    target = root / "transcripts" / step / agent
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
        records = [
            {**item, "transcript_path": relative} for item in _usage(destination, agent, model)
        ]
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
    usage_path = root / "transcripts" / step / "usage.json"
    usage_path.write_text(
        json.dumps(
            {
                "schema_version": USAGE_SCHEMA_VERSION,
                "provider": agent,
                "step": step,
                "records": usage,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "provider": agent,
        "step": step,
        "files": files,
        "usage_path": str(usage_path.relative_to(root)),
        "usage_records": len(usage),
        "usage": usage[:20000],
    }
