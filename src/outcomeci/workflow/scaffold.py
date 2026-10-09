"""The starter workflow `oci init` writes."""

from __future__ import annotations

from pathlib import Path

from outcomeci.workflow.templates import WORKFLOW_INSTRUCTIONS, WORKFLOW_REQUEST, WORKFLOW_YAML


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
