"""Redacted summaries suitable for workflow execution logs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def safe_text(value: str, limit: int = 1024) -> str:
    value = re.sub(r"(?i)(?:bearer|basic)\s+\S+", "[credential withheld]", value)
    value = re.sub(r"(?:xox[baprs]-|xapp-)[A-Za-z0-9-]+", "[credential withheld]", value)
    value = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[credential withheld]", value
    )
    value = re.sub(r"[UCDTWB][A-Z0-9]{8,}", "[provider reference withheld]", value)
    return value[:limit]


def event(
    event_type: str, step: str, capability: str, message: str, **fields: Any
) -> dict[str, Any]:
    summary = {
        "event_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "step": step,
        "capability": capability,
        "message": safe_text(message),
        "level": "info",
    }
    for name in (
        "proposal_sha256",
        "method",
        "endpoint",
        "purpose",
        "decision",
        "reason",
        "http_status",
        "ok",
        "level",
        "detail",
    ):
        value = fields.get(name)
        if value is not None:
            summary[name] = safe_text(value) if isinstance(value, str) else value
    return summary
