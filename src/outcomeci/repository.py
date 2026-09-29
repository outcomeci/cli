"""OutcomeCI repository lifecycle."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import ConfigError, compile_workflow
from .templates import (
    CONSTITUTION,
    INSTRUCTIONS,
    OUTCOME_SKILL,
    OUTCOME_YAML,
    WORKFLOW_INSTRUCTIONS,
    WORKFLOW_REQUEST,
    WORKFLOW_YAML,
)

TEMPLATES = ("workflow", "standup")


class RepositoryError(RuntimeError):
    pass


def _skill_mirror_paths(root: Path) -> tuple[Path, ...]:
    """Every agent's copy of the outcome skill. Adding a new agent's mirror
    (e.g. Codex) means adding one path here; _files() and update() both walk
    this instead of each hardcoding the pair independently."""
    return (
        root / ".agents" / "skills" / "outcome" / "SKILL.md",
        root / ".claude" / "skills" / "outcome" / "SKILL.md",
    )


def _files(root: Path) -> dict[Path, str]:
    base = root / ".outcomeci"
    values = {
        root / "outcome.yml": OUTCOME_YAML,
        base / "constitution.md": CONSTITUTION,
        **{path: OUTCOME_SKILL for path in _skill_mirror_paths(root)},
    }
    values.update({base / "instructions" / name: body for name, body in INSTRUCTIONS.items()})
    return values


def _workflow_files(root: Path) -> dict[Path, str]:
    base = root / ".outcomeci"
    values = {root / "outcome.yml": WORKFLOW_YAML, base / "request.json": WORKFLOW_REQUEST}
    values.update(
        {base / "instructions" / name: body for name, body in WORKFLOW_INSTRUCTIONS.items()}
    )
    return values


def _write_missing(root: Path, files: dict[Path, str]) -> list[str]:
    created = []
    for path, content in files.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(str(path.relative_to(root)))
    return created


def _is_v1(root: Path) -> bool:
    try:
        document = yaml.safe_load((root / "outcome.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(document, dict) and document.get("apiVersion") == "outcomeci.workflow/v1"


def initialize(root: Path, backend: str = "outcomeci", template: str = "standup") -> list[str]:
    """Write the files a template needs that do not exist yet.

    `workflow` is an outcomeci.workflow/v1 file run with `oci workflow run`;
    `standup` is the interactive v1alpha1 Standup the outcome skill drives.
    """
    if template not in TEMPLATES:
        raise RepositoryError(f"unknown template {template!r}")
    if template == "workflow":
        return _write_missing(root, _workflow_files(root))
    created = []
    (root / ".outcomeci" / "context").mkdir(parents=True, exist_ok=True)
    for path, content in _files(root).items():
        if path == root / "outcome.yml" and backend == "filesystem":
            content = content.replace("provider: outcomeci", "provider: filesystem")
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(str(path.relative_to(root)))
    return created


def update(root: Path) -> list[str]:
    """Refresh managed agent skills without replacing user-owned workflow policy."""
    if _is_v1(root):
        return _write_missing(root, _workflow_files(root))
    changed = initialize(root)
    for path in _skill_mirror_paths(root):
        if path.read_text(encoding="utf-8") != OUTCOME_SKILL:
            path.write_text(OUTCOME_SKILL, encoding="utf-8")
            relative = str(path.relative_to(root))
            if relative not in changed:
                changed.append(relative)
    return changed


def validate(root: Path) -> dict:
    if (root / ".sp").exists():
        raise RepositoryError(
            "legacy .sp context is not supported; initialize a clean OutcomeCI repository"
        )
    try:
        return compile_workflow(root / "outcome.yml")
    except ConfigError as exc:
        raise RepositoryError(str(exc)) from exc
