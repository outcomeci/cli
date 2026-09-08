"""OutcomeCI repository lifecycle."""
from __future__ import annotations

from pathlib import Path

from .config import ConfigError, compile_workflow
from .templates import CONSTITUTION, INSTRUCTIONS, OUTCOME_SKILL, OUTCOME_YAML


class RepositoryError(RuntimeError):
    pass


def _files(root: Path) -> dict[Path, str]:
    base = root / ".outcomeci"
    values = {
        root / "outcome.yml": OUTCOME_YAML,
        base / "constitution.md": CONSTITUTION,
        root / ".agents" / "skills" / "outcome" / "SKILL.md": OUTCOME_SKILL,
        root / ".claude" / "skills" / "outcome" / "SKILL.md": OUTCOME_SKILL,
    }
    values.update({base / "instructions" / name: body for name, body in INSTRUCTIONS.items()})
    return values


def initialize(root: Path, backend: str = "outcomeci") -> list[str]:
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
    """Add newly introduced framework files without replacing user policy."""
    return initialize(root)


def validate(root: Path) -> dict:
    if (root / ".sp").exists():
        raise RepositoryError("legacy .sp context is not supported; initialize a clean OutcomeCI repository")
    try:
        return compile_workflow(root / "outcome.yml")
    except ConfigError as exc:
        raise RepositoryError(str(exc)) from exc
