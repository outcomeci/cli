"""Redaction helpers. Credential material must never cross the log boundary."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(?:token|secret|authorization|auth_json|credential|password|cookie)", re.I
)
BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
TOKENISH = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[oprsu]_[A-Za-z0-9_]{8,})\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\b")
PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)


def redact(value: Any) -> Any:
    # Structured (dict/list) redaction has no production caller today --
    # redact_diagnostic() below is the only one, and it always passes a
    # string. Kept generic and tested on purpose for the next caller that
    # needs to scrub a structured payload before logging it, rather than
    # narrowed to str-only and rebuilt later.
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = PEM_BLOCK.sub("[REDACTED PEM]", value)
        value = BEARER.sub("Bearer [REDACTED]", value)
        value = JWT.sub("[REDACTED]", value)
        return TOKENISH.sub("[REDACTED]", value)
    return value


def redact_diagnostic(error: BaseException, *, max_length: int = 2000) -> str:
    """A best-effort scrubbed, length-bounded rendering of an exception for an
    operator's own workspace to read. Never a substitute for keeping genuine
    secrets out of exception messages in the first place."""
    message = redact(str(error))
    if len(message) > max_length:
        message = message[:max_length] + "…"
    return message
