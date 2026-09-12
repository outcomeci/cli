"""Validation and deterministic compilation for outcome.yml."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import __version__

RUNNERS = {"codex", "claude"}
INTERACTIONS = {"approval", "review", "consultation", "notification"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


class ConfigError(ValueError):
    pass


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _relative_path(root: Path, relative: Any, field: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ConfigError(f"{field} must be a non-empty path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"{field} escapes the repository") from exc
    return path


def _reference(root: Path, relative: str, field: str, *, json_value: bool = False) -> dict[str, Any]:
    path = _relative_path(root, relative, field)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {field} {relative}: {exc}") from exc
    if not content.strip():
        raise ConfigError(f"{field} {relative} is empty")
    result: dict[str, Any] = {"path": relative, "sha256": hashlib.sha256(content.encode()).hexdigest(), "content": content}
    if json_value:
        try:
            result["value"] = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{field} {relative} is not valid JSON") from exc
        if not isinstance(result["value"], dict):
            raise ConfigError(f"{field} {relative} must contain a JSON object")
    return result


def _agent_policy(value: Any, field: str) -> dict[str, Any]:
    item = _mapping(value or {}, field)
    runner, model = item.get("runner"), item.get("model")
    if runner is not None and runner not in RUNNERS:
        raise ConfigError(f"{field}.runner must be codex or claude")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError(f"{field}.model must be non-empty")
    return {key: item[key] for key in ("runner", "model") if item.get(key) is not None}


def _contract(value: Any, field: str, *, output: bool) -> dict[str, Any]:
    item = _mapping(value, field)
    name, media_type = item.get("name"), item.get("media_type")
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ConfigError(f"{field}.name must be a valid identifier")
    if not isinstance(media_type, str) or "/" not in media_type:
        raise ConfigError(f"{field}.media_type is required")
    result = {"name": name, "media_type": media_type, "required": item.get("required", True)}
    if not isinstance(result["required"], bool):
        raise ConfigError(f"{field}.required must be true or false")
    if output:
        path = item.get("path")
        if not isinstance(path, str) or not path.strip() or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ConfigError(f"{field}.path must remain within the run directory")
        result["path"] = Path(path).as_posix()
    else:
        source = item.get("from")
        if not isinstance(source, str) or not source.strip():
            raise ConfigError(f"{field}.from is required")
        result["from"] = source
    if item.get("schema") is not None:
        result["schema"] = item["schema"]
    if isinstance(item.get("description"), str) and item["description"].strip():
        result["description"] = item["description"].strip()
    return result


def _human_interactions(value: Any, field: str) -> dict[str, list[dict[str, Any]]]:
    humans = _mapping(value or {}, field)
    unknown = set(humans) - {"before", "during", "after"}
    if unknown:
        raise ConfigError(f"{field} has unknown timing: {', '.join(sorted(unknown))}")
    result: dict[str, list[dict[str, Any]]] = {"before": [], "during": [], "after": []}
    seen: set[str] = set()
    for timing in result:
        entries = humans.get(timing, [])
        if not isinstance(entries, list):
            raise ConfigError(f"{field}.{timing} must be a list")
        for index, value in enumerate(entries):
            item = _mapping(value, f"{field}.{timing}[{index}]")
            interaction_id = item.get("id")
            if not isinstance(interaction_id, str) or not IDENTIFIER.fullmatch(interaction_id) or interaction_id in seen:
                raise ConfigError(f"human interaction ids must be unique valid identifiers in {field}")
            seen.add(interaction_id)
            participant = item.get("participant")
            if isinstance(participant, str):
                participant = {"role": participant}
            participant = _mapping(participant, f"{field}.{timing}[{index}].participant")
            if not isinstance(participant.get("role"), str) or not participant["role"].strip():
                raise ConfigError(f"{field}.{timing}[{index}].participant.role is required")
            interaction = item.get("interaction")
            if interaction not in INTERACTIONS:
                raise ConfigError(f"{field}.{timing}[{index}].interaction is unsupported")
            required = item.get("required", interaction != "notification")
            if not isinstance(required, bool):
                raise ConfigError(f"{field}.{timing}[{index}].required must be true or false")
            purpose = item.get("purpose")
            if not isinstance(purpose, str) or not purpose.strip():
                raise ConfigError(f"{field}.{timing}[{index}].purpose is required")
            delivery = _mapping(item.get("delivery", {"type": "local"}), f"{field}.{timing}[{index}].delivery")
            if delivery.get("type") not in {"local", "slack"}:
                raise ConfigError(f"{field}.{timing}[{index}].delivery.type is unsupported")
            targets = delivery.get("targets", [])
            if not isinstance(targets, list):
                raise ConfigError(f"{field}.{timing}[{index}].delivery.targets must be a list")
            normalized_targets = []
            for target_index, target_value in enumerate(targets):
                target = _mapping(target_value, f"{field}.{timing}[{index}].delivery.targets[{target_index}]")
                if target.get("kind") not in {"user", "channel", "group"}:
                    raise ConfigError(f"{field}.{timing}[{index}].delivery.targets[{target_index}].kind is unsupported")
                if not isinstance(target.get("name"), str) or not target["name"].strip():
                    raise ConfigError(f"{field}.{timing}[{index}].delivery.targets[{target_index}].name is required")
                normalized_targets.append({"kind": target["kind"], "name": target["name"].strip().lstrip("@#")})
            if normalized_targets:
                delivery["targets"] = normalized_targets
            wait = _mapping(item.get("wait", {"strategy": "ask"}), f"{field}.{timing}[{index}].wait")
            if wait.get("strategy") not in {"ask", "block", "continue"}:
                raise ConfigError(f"{field}.{timing}[{index}].wait.strategy is unsupported")
            if wait.get("timeout_seconds") is not None and (not isinstance(wait["timeout_seconds"], int) or not 0 <= wait["timeout_seconds"] <= 86400):
                raise ConfigError(f"{field}.{timing}[{index}].wait.timeout_seconds must be between 0 and 86400")
            normalized = {"id": interaction_id, "participant": participant, "purpose": purpose.strip(), "interaction": interaction, "required": required, "delivery": delivery}
            normalized["wait"] = {"strategy": wait["strategy"], **({"timeout_seconds": wait["timeout_seconds"]} if wait.get("timeout_seconds") is not None else {})}
            if timing == "during":
                availability = item.get("availability", "on_demand")
                if availability not in {"on_demand", "always"}:
                    raise ConfigError(f"{field}.{timing}[{index}].availability is unsupported")
                normalized["availability"] = availability
            result[timing].append(normalized)
    return result


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
    if len(instructions) != 1:
        raise ConfigError("spec.instructions must define exactly one orchestrator")
    orchestrator_name, orchestrator_value = next(iter(instructions.items()))
    if not IDENTIFIER.fullmatch(str(orchestrator_name)):
        raise ConfigError("the orchestrator name must be a valid identifier")
    if isinstance(orchestrator_value, str):
        orchestrator_value = {"path": orchestrator_value}
        instructions[orchestrator_name] = orchestrator_value
    orchestrator = _mapping(orchestrator_value, f"spec.instructions.{orchestrator_name}")
    if not isinstance(orchestrator.get("path"), str):
        raise ConfigError(f"spec.instructions.{orchestrator_name}.path is required")
    _agent_policy(orchestrator, f"spec.instructions.{orchestrator_name}")

    agents = _mapping(spec.get("agents", {}), "spec.agents")
    default = _agent_policy(agents.get("default", {}), "spec.agents.default")
    phases = _mapping(agents.get("phases", {}), "spec.agents.phases")
    if not phases:
        raise ConfigError("spec.agents.phases must define at least one phase")
    outputs: dict[tuple[str, str], dict[str, Any]] = {}
    output_paths: set[str] = set()
    normalized_phases: dict[str, Any] = {}
    for phase_name, raw_policy in phases.items():
        if not isinstance(phase_name, str) or not IDENTIFIER.fullmatch(phase_name):
            raise ConfigError(f"invalid outcome phase: {phase_name}")
        field = f"spec.agents.phases.{phase_name}"
        policy = _mapping(raw_policy, field)
        if not isinstance(policy.get("instructions"), str):
            raise ConfigError(f"{field}.instructions is required")
        _agent_policy(policy, field)
        needs = policy.get("needs", [])
        if not isinstance(needs, list) or not all(isinstance(item, str) for item in needs) or len(needs) != len(set(needs)):
            raise ConfigError(f"{field}.needs must be a list of unique phase names")
        expects = _mapping(policy.get("expects", {}), f"{field}.expects")
        raw_inputs, raw_outputs = expects.get("inputs", []), expects.get("outputs", [])
        if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
            raise ConfigError(f"{field}.expects inputs and outputs must be lists")
        inputs = [_contract(item, f"{field}.expects.inputs[{index}]", output=False) for index, item in enumerate(raw_inputs)]
        phase_outputs = [_contract(item, f"{field}.expects.outputs[{index}]", output=True) for index, item in enumerate(raw_outputs)]
        if len({item["name"] for item in inputs}) != len(inputs) or len({item["name"] for item in phase_outputs}) != len(phase_outputs):
            raise ConfigError(f"{field} contract names must be unique")
        for item in phase_outputs:
            if item["path"] in output_paths:
                raise ConfigError(f"duplicate output path: {item['path']}")
            output_paths.add(item["path"])
            outputs[(phase_name, item["name"])] = item
        humans = _human_interactions(policy.get("humans", {}), f"{field}.humans")
        normalized_phases[phase_name] = {"needs": needs, "inputs": inputs, "outputs": phase_outputs, "humans": humans}

    for phase_name, phase in normalized_phases.items():
        for dependency in phase["needs"]:
            if dependency == phase_name:
                raise ConfigError(f"phase {phase_name} cannot depend on itself")
            if dependency not in phases:
                raise ConfigError(f"phase {phase_name} needs unknown phase {dependency}")
        for item in phase["inputs"]:
            source = item["from"]
            if source.startswith(("runtime.", "context.")):
                continue
            match = re.fullmatch(r"([a-z][a-z0-9_-]{0,62})\.outputs\.([a-z][a-z0-9_-]{0,62})", source)
            if not match or (match.group(1), match.group(2)) not in outputs:
                raise ConfigError(f"input {phase_name}.{item['name']} has no declared producer: {source}")
            if match.group(1) not in phase["needs"]:
                raise ConfigError(f"input {phase_name}.{item['name']} must come from a direct dependency")

    indegree = {name: len(value["needs"]) for name, value in normalized_phases.items()}
    remaining = set(normalized_phases)
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(name for name in remaining if indegree[name] == 0)
        if not ready:
            raise ConfigError("outcome phase graph contains a cycle")
        levels.append(ready)
        remaining.difference_update(ready)
        for name in remaining:
            indegree[name] -= sum(dependency in ready for dependency in normalized_phases[name]["needs"])

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
    root["_graph"] = {"orchestrator": orchestrator_name, "levels": levels, "phases": normalized_phases, "default_policy": default}
    return root


def _excluded(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) or (pattern.endswith("/**") and (relative == pattern[:-3] or relative.startswith(pattern[:-2]))) for pattern in patterns)


def _filesystem_context(root: Path, context: dict[str, Any]) -> list[dict[str, Any]]:
    paths: dict[str, Path] = {}
    for pattern in context.get("include", []):
        matches = (root / pattern[:-3]).rglob("*") if pattern.endswith("/**") and (root / pattern[:-3]).is_dir() else root.glob(pattern)
        for path in matches:
            if path.is_file():
                relative = path.resolve().relative_to(root.resolve()).as_posix()
                if not _excluded(relative, context.get("exclude", [])):
                    paths[relative] = path.resolve()
    if len(paths) > 5000:
        raise ConfigError("filesystem context exceeds 5000 files")
    return [{"path": relative, "byte_size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for relative, path in sorted(paths.items())]


def compile_workflow(path: Path) -> dict[str, Any]:
    document = load(path)
    graph = document.pop("_graph")
    spec, root = document["spec"], path.parent
    orchestrator_name = graph["orchestrator"]
    orchestrator_config = spec["instructions"][orchestrator_name]
    orchestrator = _reference(root, orchestrator_config["path"], f"spec.instructions.{orchestrator_name}.path")
    default = graph["default_policy"]
    orchestrator["name"] = orchestrator_name
    orchestrator["policy"] = {"runner": orchestrator_config.get("runner", default.get("runner")), "model": orchestrator_config.get("model", default.get("model"))}
    phases: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    for phase_name, contract in graph["phases"].items():
        policy = spec["agents"]["phases"][phase_name]
        phases[phase_name] = {**_reference(root, policy["instructions"], f"spec.agents.phases.{phase_name}.instructions"), "needs": contract["needs"], "expects": {"inputs": contract["inputs"], "outputs": contract["outputs"]}, "humans": contract["humans"], "policy": {"runner": policy.get("runner", default.get("runner")), "model": policy.get("model", default.get("model"))}}
        for item in (*contract["inputs"], *contract["outputs"]):
            if item.get("schema") and item["schema"] not in schemas:
                schemas[item["schema"]] = _reference(root, item["schema"], "artifact schema", json_value=True)
    normalized = json.loads(json.dumps(document, sort_keys=True, separators=(",", ":")))
    context = spec.get("context", {"provider": "outcomeci"})
    context_files = _filesystem_context(root, context) if context.get("provider") == "filesystem" else []
    # `standup` is a compatibility alias for the runtime while callers migrate
    # to the role-neutral orchestrator key.
    resolved = {"orchestrator": orchestrator, "standup": orchestrator, "phases": phases, "schemas": schemas}
    revision_input = {"workflow": normalized, "graph": {"levels": graph["levels"]}, "instructions": resolved, "context": {"provider": context.get("provider", "outcomeci"), "files": context_files}}
    revision = hashlib.sha256(json.dumps(revision_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": "outcomeci.workflow/v1alpha1", "engine_version": "2", "engine_package_version": __version__, "workflow_revision": revision, **revision_input}
