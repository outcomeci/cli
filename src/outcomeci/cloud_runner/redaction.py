"""Redaction helpers. Credential material must never cross the log boundary."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY = re.compile(r"(?:token|secret|authorization|auth_json|credential|password|cookie)", re.I)
BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
TOKENISH = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[opsu]_[A-Za-z0-9_]{8,})\b")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return TOKENISH.sub("[REDACTED]", BEARER.sub("Bearer [REDACTED]", value))
    return value
