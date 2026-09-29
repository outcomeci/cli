"""OutcomeCI repository lifecycle."""

from __future__ import annotations

from pathlib import Path

from .config import ConfigError, compile_workflow
from .templates import WORKFLOW_INSTRUCTIONS, WORKFLOW_REQUEST, WORKFLOW_YAML


class RepositoryError(RuntimeError):
    pass


def _files(root: Path) -> dict[Path, str]:
    base = root / ".outcomeci"
    values = {root / "outcome.yml": WORKFLOW_YAML, base / "request.json": WORKFLOW_REQUEST}
    values.update(
        {base / "instructions" / name: body for name, body in WORKFLOW_INSTRUCTIONS.items()}
    )
    return values


def initialize(root: Path) -> list[str]:
    """Write the starter workflow's files that do not exist yet."""
    created = []
    for path, content in _files(root).items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(str(path.relative_to(root)))
    return created


def validate(root: Path) -> dict:
    try:
        return compile_workflow(root / "outcome.yml")
    except ConfigError as exc:
        raise RepositoryError(str(exc)) from exc
