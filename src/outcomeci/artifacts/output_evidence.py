"""Bounded reads of retained output evidence without filesystem traversal."""

from pathlib import Path

from outcomeci.runtime.process import ExecutionError


def safe_path(root: Path, reference: str, area: str = "outcomes") -> Path:
    if not isinstance(reference, str):
        raise ExecutionError("historical reference must be a string")
    base = root.resolve() / ".outcomeci" / area
    candidate = root.resolve() / reference
    path = candidate.resolve()
    if not path.is_relative_to(base) or path != candidate.absolute():
        raise ExecutionError("historical path escapes its storage area or uses a symlink")
    return path


def read_bytes(path: Path, limit: int = 16 * 1024 * 1024) -> bytes:
    if path.stat().st_size > limit:
        raise ExecutionError("output-history evidence exceeds its read limit")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ExecutionError("output-history evidence exceeds its read limit")
    return value
