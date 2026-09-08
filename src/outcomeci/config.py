"""Validation and deterministic compilation for outcome.yml."""
from __future__ import annotations

import hashlib
import fnmatch
import json
from pathlib import Path
from typing import Any

import yaml

from . import __version__

PHASES = ("intake", "plan", "tasks", "implementation", "pr")
RUNNERS = {"codex", "claude"}


class ConfigError(ValueError):
    pass


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def load(path: Path) -> dict[str, Any]:
    try:
        root = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "document")
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if root.get("apiVersion") != "outcomeci.dev/v1alpha1":
        raise ConfigError("apiVersion must be outcomeci.dev/v1alpha1")
    if root.get("kind") != "OutcomeWorkflow":
        raise ConfigError("kind must be OutcomeWorkflow")
    metadata = _mapping(root.get("metadata"), "metadata")
    if not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
        raise ConfigError("metadata.name is required")
    spec = _mapping(root.get("spec"), "spec")
    for field, choices in (("backend", {"outcomeci", "filesystem"}), ("context", {"outcomeci", "http", "filesystem"})):
        value = _mapping(spec.get(field, {"provider": "outcomeci"}), f"spec.{field}")
        if value.get("provider", "outcomeci") not in choices:
            raise ConfigError(f"unsupported spec.{field}.provider")
        if field == "context":
            for patterns_name in ("include", "exclude"):
                patterns = value.get(patterns_name, [])
                if not isinstance(patterns, list) or not all(isinstance(pattern, str) and pattern.strip() for pattern in patterns):
                    raise ConfigError(f"spec.context.{patterns_name} must be a list of non-empty glob strings")
    instructions = _mapping(spec.get("instructions"), "spec.instructions")
    if not isinstance(instructions.get("standup"), str):
        raise ConfigError("spec.instructions.standup is required")
    agents = _mapping(spec.get("agents", {}), "spec.agents")
    default = _mapping(agents.get("default", {}), "spec.agents.default")
    phases = _mapping(agents.get("phases", {}), "spec.agents.phases")
    unknown = set(phases) - set(PHASES)
    if unknown:
        raise ConfigError(f"unknown outcome phases: {', '.join(sorted(unknown))}")
    for name, policy in (("default", default), *phases.items()):
        item = _mapping(policy, f"spec.agents.{name}")
        if item.get("runner") is not None and item["runner"] not in RUNNERS:
            raise ConfigError(f"spec.agents.{name}.runner must be codex or claude")
        if item.get("model") is not None and not str(item["model"]).strip():
            raise ConfigError(f"spec.agents.{name}.model must be non-empty")
        if name != "default" and not isinstance(item.get("instructions"), str):
            raise ConfigError(f"spec.agents.{name}.instructions is required")
    connections = spec.get("connections", [])
    if not isinstance(connections, list):
        raise ConfigError("spec.connections must be a list")
    refs: set[str] = set()
    for index, value in enumerate(connections):
        item = _mapping(value, f"spec.connections[{index}]")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref or ref in refs:
            raise ConfigError("connection references must be unique non-empty strings")
        refs.add(ref)
    return root


def _instruction(root: Path, relative: str) -> dict[str, str]:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError("instruction path escapes repository") from exc
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read instruction {relative}: {exc}") from exc
    if not content.strip():
        raise ConfigError(f"instruction {relative} is empty")
    return {"path": relative, "sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content}


def _excluded(relative: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern)
        or (pattern.endswith("/**") and (relative == pattern[:-3] or relative.startswith(pattern[:-2])))
        for pattern in patterns
    )


def _filesystem_context(root: Path, context: dict[str, Any]) -> list[dict[str, Any]]:
    includes = context.get("include", [])
    excludes = context.get("exclude", [])
    paths: dict[str, Path] = {}
    for pattern in includes:
        if pattern.endswith("/**"):
            base = root / pattern[:-3]
            matches = base.rglob("*") if base.is_dir() else []
        else:
            matches = root.glob(pattern)
        for path in matches:
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                relative = resolved.relative_to(root.resolve()).as_posix()
            except ValueError as exc:
                raise ConfigError(f"context path escapes repository: {path}") from exc
            if not _excluded(relative, excludes):
                paths[relative] = resolved
    if len(paths) > 5000:
        raise ConfigError("filesystem context exceeds 5000 files")
    return [
        {"path": relative, "byte_size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for relative, path in sorted(paths.items())
    ]


def compile_workflow(path: Path) -> dict[str, Any]:
    document = load(path)
    spec = document["spec"]
    resolved = {"standup": _instruction(path.parent, spec["instructions"]["standup"]), "phases": {}}
    for phase, policy in spec.get("agents", {}).get("phases", {}).items():
        resolved["phases"][phase] = _instruction(path.parent, policy["instructions"])
    normalized = json.loads(json.dumps(document, sort_keys=True, separators=(",", ":")))
    context = spec.get("context", {"provider": "outcomeci"})
    context_files = _filesystem_context(path.parent, context) if context.get("provider") == "filesystem" else []
    revision_input = {"workflow": normalized, "instructions": resolved, "context": {"provider": context.get("provider", "outcomeci"), "files": context_files}}
    revision = hashlib.sha256(json.dumps(revision_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": "outcomeci.workflow/v1alpha1", "engine_version": "1", "engine_package_version": __version__, "workflow_revision": revision, **revision_input}
