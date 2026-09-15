"""Filesystem-backed execution of an OutcomeWorkflow with a local agent."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as validate_json

from .capability import serve as serve_capability
from .config import compile_workflow
from .contracts import FORMAT_CHECKER, ContractError, validate_trigger_payload
from .manifest import build_manifest
from .outcome import (
    _select_sessions,
    _session_details,
    _transcripts,
    _validate_trajectory,
)
from .process import ExecutionError, invoke


def _id(intent: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", intent.casefold()).strip("-")[:36] or "outcome"
    return f"{stamp}-{slug}"


def _policy(
    compiled: dict[str, Any], phase: str, agent: str | None, model: str | None
) -> tuple[str, str | None]:
    selected = compiled["instructions"]["phases"].get(phase, {}).get("policy", {})
    runner = agent or selected.get("runner")
    chosen_model = model or selected.get("model")
    if runner not in {"codex", "claude", "opencode"}:
        raise ExecutionError(f"no supported agent configured for {phase}")
    if runner == "opencode" and (
        not isinstance(chosen_model, str) or not chosen_model.startswith("openrouter/")
    ):
        raise ExecutionError("OpenCode needs an explicit openrouter/<model> selection")
    return runner, chosen_model


def _ready(compiled: dict[str, Any], completed: list[str]) -> list[str]:
    done = set(completed)
    return sorted(
        name
        for name, phase in compiled["instructions"]["phases"].items()
        if name not in done and set(phase["needs"]) <= done
    )


def _phase_states(compiled: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    completed = set(state.get("completed_phases", []))
    ready = set(_ready(compiled, list(completed)))
    return {
        name: "completed"
        if name in completed
        else "active"
        if name == state.get("phase") and state.get("status") == "running"
        else "queued"
        if name in ready
        else "blocked"
        for name in compiled["instructions"]["phases"]
    }


def _artifact_path(outcome_root: Path, contract: dict[str, Any]) -> Path:
    path = (outcome_root / contract["path"]).resolve()
    try:
        path.relative_to(outcome_root.resolve())
    except ValueError as exc:
        raise ExecutionError(f"artifact {contract['name']} escapes the outcome directory") from exc
    return path


def _prepare_writable_artifacts(
    compiled: dict[str, Any], outcome_root: Path, phase: str
) -> list[Path]:
    standup = outcome_root / "standup.md"
    standup.parent.mkdir(parents=True, exist_ok=True)
    standup.touch(exist_ok=True)
    paths = [standup]
    for contract in compiled["instructions"]["phases"][phase]["expects"]["outputs"]:
        path = _artifact_path(outcome_root, contract)
        if contract["media_type"] == "inode/directory":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        paths.append(path)
    return paths


def _validate_outputs(compiled: dict[str, Any], outcome_root: Path, phase: str) -> None:
    for contract in compiled["instructions"]["phases"][phase]["expects"]["outputs"]:
        path = _artifact_path(outcome_root, contract)
        if not path.exists():
            if contract["required"]:
                raise ExecutionError(
                    f"missing required output {phase}.{contract['name']}: {contract['path']}"
                )
            continue
        media_type = contract["media_type"]
        if media_type == "inode/directory":
            if not path.is_dir() or not any(item.is_file() for item in path.rglob("*")):
                raise ExecutionError(
                    f"output {phase}.{contract['name']} must be a non-empty directory"
                )
            continue
        if not path.is_file() or not path.read_bytes():
            raise ExecutionError(f"output {phase}.{contract['name']} must be a non-empty file")
        if media_type == "application/json" or contract.get("schema"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if contract.get("schema"):
                    validate_json(
                        value,
                        compiled["instructions"]["schemas"][contract["schema"]]["value"],
                        format_checker=FORMAT_CHECKER,
                    )
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise ExecutionError(
                    f"output {phase}.{contract['name']} failed JSON validation: {exc}"
                ) from exc
        elif media_type.startswith("text/"):
            try:
                if not path.read_text(encoding="utf-8").strip():
                    raise ExecutionError(f"output {phase}.{contract['name']} must contain text")
            except UnicodeDecodeError as exc:
                raise ExecutionError(
                    f"output {phase}.{contract['name']} is not UTF-8 text"
                ) from exc


def _input_context(
    compiled: dict[str, Any], outcome_root: Path, phase: str, state: dict[str, Any]
) -> list[dict[str, Any]]:
    values = []
    for contract in compiled["instructions"]["phases"][phase]["expects"]["inputs"]:
        source = contract["from"]
        value: dict[str, Any] = {**contract}
        if source == "runtime.intent":
            value["value"] = state["intent"]
        elif source.startswith("trigger."):
            trigger = state.get("trigger") or {}
            name = source.removeprefix("trigger.")
            if trigger.get("name") != name:
                if contract["required"]:
                    raise ExecutionError(
                        f"required input {phase}.{contract['name']} is unavailable"
                    )
            else:
                value["value"] = trigger["value"]
        elif ".outputs." in source:
            producer, output_name = source.split(".outputs.", 1)
            output = next(
                item
                for item in compiled["instructions"]["phases"][producer]["expects"]["outputs"]
                if item["name"] == output_name
            )
            path = _artifact_path(outcome_root, output)
            if contract["required"] and not path.exists():
                raise ExecutionError(f"required input {phase}.{contract['name']} is unavailable")
            value["path"] = str(path)
            if path.exists() and contract.get("schema"):
                value["value"] = json.loads(path.read_text(encoding="utf-8"))
        if "value" in value and contract.get("schema"):
            try:
                validate_json(
                    value["value"],
                    compiled["instructions"]["schemas"][contract["schema"]]["value"],
                    format_checker=FORMAT_CHECKER,
                )
            except ValidationError as exc:
                raise ExecutionError(
                    f"input {phase}.{contract['name']} failed schema validation"
                ) from exc
        values.append(value)
    return values


def _human_context(state: dict[str, Any], outcome_root: Path) -> list[dict[str, Any]]:
    context = []
    paths = {
        Path(item["path"])
        for item in state.get("interaction_history", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    paths.update((outcome_root / "interactions").glob("*/*.json"))
    for path in sorted(paths):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        context.append(
            {
                "phase": value.get("phase"),
                "timing": value.get("timing"),
                "interaction_id": value.get("id"),
                "status": value.get("status"),
                "response": value.get("response"),
                "observed_responses": value.get("observed_responses", []),
            }
        )
    return context


def _interaction_path(root: Path, run_id: str, phase: str, interaction_id: str) -> Path:
    return (
        root
        / ".outcomeci"
        / "outcomes"
        / run_id
        / "interactions"
        / phase
        / f"{interaction_id}.json"
    )


def _open_interaction(
    root: Path, state: dict[str, Any], phase: str, timing: str, definition: dict[str, Any]
) -> dict[str, Any]:
    request = {
        "schema_version": 1,
        "run_id": state["run_id"],
        "phase": phase,
        "timing": timing,
        "status": "pending",
        "requested_at": datetime.now(UTC).isoformat(),
        **definition,
    }
    path = _interaction_path(root, state["run_id"], phase, definition["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state.update(
        {
            "status": "awaiting_input",
            "pending_interaction": {
                "phase": phase,
                "timing": timing,
                "id": definition["id"],
                "path": str(path),
            },
        }
    )
    _write(root, state)
    return state


def _first_required_interaction(
    compiled: dict[str, Any], phase: str, timing: str, state: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    resolved = {
        item["id"]
        for item in (state or {}).get("interaction_history", [])
        if item.get("phase") == phase
        and item.get("timing") == timing
        and item.get("status") in {"approved", "answered"}
    }
    return next(
        (
            item
            for item in compiled["instructions"]["phases"][phase]["humans"][timing]
            if item["required"] and item["id"] not in resolved
        ),
        None,
    )


def _record(root: Path, run_id: str) -> Path:
    return root / ".outcomeci" / "outcomes" / run_id / "run.json"


def _read(root: Path, run_id: str) -> dict[str, Any]:
    try:
        value = json.loads(_record(root, run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"local outcome {run_id!r} was not found") from exc
    if not isinstance(value, dict):
        raise ExecutionError("invalid local outcome state")
    return value


def _write(root: Path, state: dict[str, Any]) -> None:
    path = _record(root, state["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _local_revision(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _interactive_session(root: Path, compiled: dict[str, Any]) -> dict[str, Any]:
    codex_id = os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    claude_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if codex_id:
        provider, session_id = "codex", codex_id
    elif claude_id or os.environ.get("CLAUDECODE"):
        provider, session_id = "claude", claude_id
    else:
        provider = (
            compiled["workflow"]["spec"].get("agents", {}).get("default", {}).get("runner", "codex")
        )
        session_id = None
    matches = _select_sessions(provider, session_id, root, None)
    if matches and not session_id:
        session_id = _session_details(matches[0], provider)[0]
    return {
        "provider": provider,
        "session_id": session_id,
        "byte_offset": matches[0].stat().st_size if matches else 0,
    }


def _execute(
    root: Path,
    config: Path,
    state: dict[str, Any],
    *,
    agent: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    compiled = compile_workflow(config)
    if compiled["workflow"]["spec"]["backend"].get("provider") != "filesystem":
        raise ExecutionError("local execution requires spec.backend.provider: filesystem")
    phase = state["phase"]
    runner, chosen_model = _policy(compiled, phase, agent, model)
    outcome_root = root / ".outcomeci" / "outcomes" / state["run_id"]
    writable_artifacts = _prepare_writable_artifacts(compiled, outcome_root, phase)
    connection_secrets = {
        name
        for connection in compiled["workflow"]["spec"].get("connections", [])
        if isinstance(connection, dict) and isinstance(connection.get("auth"), dict)
        for name in [
            connection["auth"].get("env")
            or (
                str(connection["auth"].get("credential", "")).removeprefix("env:")
                if str(connection["auth"].get("credential", "")).startswith("env:")
                else ""
            )
        ]
        if isinstance(name, str) and name
    }
    repository = root.name
    shared = compiled["instructions"]["orchestrator"]["content"]
    instructions = compiled["instructions"]["phases"][phase]["content"]
    local_revision = f"filesystem:{compiled['workflow_revision']}"
    context = {
        "run_id": state["run_id"],
        "phase": phase,
        "intent": state["intent"],
        "repository": {"name": repository, "checkout": str(root)},
        "prior_phases": state.get("completed_phases", []),
        "workflow_revision": compiled["workflow_revision"],
        "context_files": compiled["context"]["files"],
        "inputs": _input_context(compiled, outcome_root, phase, state),
        "with": compiled["instructions"]["phases"][phase]["with"],
        "outputs": compiled["instructions"]["phases"][phase]["expects"]["outputs"],
        "capabilities": compiled["instructions"]["phases"][phase].get("capabilities", []),
        "human_context": _human_context(state, outcome_root),
    }
    intake_contract = ""
    if phase == "intake":
        intake_contract = f"""
Write intake/trajectory.json with schema_version \"1\", ontology_revision_id
\"{local_revision}\", and at least one target. The local target must use
repository_id \"local:{repository}\", repository \"{repository}\", a non-empty
rationale, and a candidates array following the stable role and disposition
contract above. Use paths relative to this repository.
"""
    prompt = f"{shared}\n\n{instructions}\n\nThis is a filesystem-backed local Standup. Work in {root}. Write durable artifacts beneath {outcome_root}. During intake, plan, and tasks, do not modify product source files. There is no OutcomeCI Cloud or Digital Twin; inspect the local repository directly. Only execute API capabilities listed for this phase, using `oci integration execute <capability> --phase {phase} --input-stdin`; the capability broker owns credentials and authorization. Only use human tools for a hook declared on this current phase with Slack or custom delivery and configured targets. Never discover targets or change hook assignments during execution. Use only readable names; never request or expose provider IDs. Before a wired hook with wait strategy `ask`, ask the requester how long to wait or whether to continue. Deliver it with `oci human request <interaction-id> --run {state['run_id']} --workspace {root}`; add `--continue` only when the requester chose to keep working. Otherwise poll for exactly their bounded duration using `oci human poll <interaction-id> --run {state['run_id']} --wait <seconds> --workspace {root}`. Apply a received response with `oci human accept` and preserve it as outcome context.\n{intake_contract}\n{json.dumps(context, separators=(',', ':'))}"
    runtime_cli = shlex.join([sys.executable, "-m", "outcomeci.cli"])
    prompt += f"\nThe authoritative CLI for this run is `{runtime_cli}`. Use this absolute command instead of bare `oci` in every tool invocation; login shells may select an older globally installed CLI. For API requests use `{runtime_cli} integration execute <capability> --phase {phase} --input-stdin`. Do not fall back to a global CLI."
    state.update(
        {
            "status": "running",
            "agent": runner,
            "model": chosen_model,
            "workflow_revision": compiled["workflow_revision"],
        }
    )
    _write(root, state)
    phase_started_at = datetime.now(UTC).isoformat()
    try:
        with serve_capability(
            root, config, state["run_id"], phase, compiled=compiled
        ) as capability_env:
            summary = invoke(
                runner,
                chosen_model,
                prompt,
                root,
                7200,
                allow_local_auth=True,
                extra_env=capability_env,
                writable_paths=writable_artifacts,
                excluded_env=connection_secrets,
            )
        persisted = _read(root, state["run_id"])
        if persisted.get("status") == "awaiting_input":
            return persisted
        _validate_outputs(compiled, outcome_root, phase)
        if phase == "intake":
            trajectory = json.loads(
                (outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8")
            )
            _validate_trajectory(
                trajectory, {"intent_context": {"ontology_revision_id": local_revision}}
            )
        transcripts = _transcripts(
            runner, outcome_root, phase, workspace=root, since=phase_started_at
        )
    except (ExecutionError, OSError, json.JSONDecodeError) as exc:
        state.update({"status": "error", "error": str(exc)})
        state["phases"] = _phase_states(compiled, state)
        _write(root, state)
        if isinstance(exc, ExecutionError):
            raise
        raise ExecutionError(f"invalid local outcome artifacts: {exc}") from exc
    after = _first_required_interaction(compiled, phase, "after", state)
    if after:
        state["phase_output_ready"] = True
    else:
        state["completed_phases"] = [*state.get("completed_phases", []), phase]
        state["status"] = (
            "awaiting_confirmation" if phase != "tasks" else "ready_for_implementation"
        )
    state["summary"] = summary[-1000:]
    state["usage_records"] = transcripts["usage_records"]
    state.pop("error", None)
    constitution = root / ".outcomeci" / "constitution.md"
    manifest = build_manifest(
        outcome_root=outcome_root,
        artifact_base=root,
        run_id=state["run_id"],
        workflow_run_id=None,
        trajectory_version=None,
        phase=phase,
        workflow_revision=compiled["workflow_revision"],
        backend_provider="filesystem",
        state_repository=None,
        context_provider=compiled["workflow"]["spec"]["context"].get("provider", "filesystem"),
        context_revision_id=local_revision,
        constitution_sha256=hashlib.sha256(
            constitution.read_bytes() if constitution.exists() else b""
        ).hexdigest(),
        repository_base_commits={repository: _local_revision(root)},
        runner=runner,
        model=chosen_model,
        transcript=transcripts,
        phase_contract=compiled["instructions"]["phases"][phase]["expects"],
    )
    (outcome_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if after:
        return _open_interaction(root, state, phase, "after", after)
    state["ready_phases"] = _ready(compiled, state["completed_phases"])
    state["phases"] = _phase_states(compiled, state)
    _write(root, state)
    return state


def start(
    root: Path, config: Path, intent: str, *, agent: str | None = None, model: str | None = None
) -> dict[str, Any]:
    if not intent.strip():
        raise ExecutionError("intent is required")
    compiled = compile_workflow(config)
    if not any(trigger["type"] == "manual" for trigger in compiled["triggers"].values()):
        raise ExecutionError("workflow does not declare a manual trigger")
    first = _ready(compiled, [])[0]
    state = {
        "schema_version": 2,
        "run_id": _id(intent),
        "intent": intent.strip(),
        "phase": first,
        "status": "queued",
        "completed_phases": [],
        "ready_phases": _ready(compiled, []),
        "created_at": datetime.now(UTC).isoformat(),
    }
    before = _first_required_interaction(compiled, first, "before", state)
    if before:
        return _open_interaction(root, state, first, "before", before)
    return _execute(root, config, state, agent=agent, model=model)


def trigger(
    root: Path,
    config: Path,
    trigger_name: str,
    payload: dict[str, Any],
    *,
    agent: str | None = None,
    model: str | None = None,
    on_created: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Validate and materialize a named trigger before any agent execution."""
    compiled = compile_workflow(config)
    definition = compiled["triggers"].get(trigger_name)
    if definition is None:
        raise ExecutionError(f"workflow does not declare trigger {trigger_name}")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    limit = 2 * 1024 * 1024 if definition["type"] == "webhook.received" else 1024 * 1024
    if len(encoded) > limit:
        raise ExecutionError("trigger payload exceeds the 1 MiB local limit")
    try:
        validate_trigger_payload(definition["type"], payload)
    except ContractError as exc:
        raise ExecutionError(str(exc)) from exc
    payload = json.loads(encoded)
    subject = payload.get("subject")
    intent = (
        subject
        if isinstance(subject, str) and subject.strip()
        else f"{definition['type']} received"
    )
    first = _ready(compiled, [])[0]
    state = {
        "schema_version": 2,
        "run_id": _id(intent),
        "intent": intent,
        "trigger": {"name": trigger_name, "type": definition["type"], "value": payload},
        "phase": first,
        "status": "queued",
        "completed_phases": [],
        "ready_phases": _ready(compiled, []),
        "created_at": datetime.now(UTC).isoformat(),
    }
    if on_created is not None:
        _write(root, state)
        on_created(state["run_id"])
    before = _first_required_interaction(compiled, first, "before", state)
    if before:
        return _open_interaction(root, state, first, "before", before)
    return _execute(root, config, state, agent=agent, model=model)


def begin(root: Path, config: Path, intent: str) -> dict[str, Any]:
    """Create an interactive run without launching a child agent."""
    if not intent.strip():
        raise ExecutionError("intent is required")
    compiled = compile_workflow(config)
    if not any(trigger["type"] == "manual" for trigger in compiled["triggers"].values()):
        raise ExecutionError("workflow does not declare a manual trigger")
    if compiled["workflow"]["spec"]["backend"].get("provider") != "filesystem":
        raise ExecutionError(
            "interactive local execution requires spec.backend.provider: filesystem"
        )
    session = _interactive_session(root, compiled)
    state = {
        "schema_version": 2,
        "run_id": _id(intent),
        "intent": intent.strip(),
        "phase": _ready(compiled, [])[0],
        "status": "awaiting_agent",
        "completed_phases": [],
        "workflow_revision": compiled["workflow_revision"],
        "interactive_session": session,
        "created_at": datetime.now(UTC).isoformat(),
    }
    state["ready_phases"] = _ready(compiled, [])
    state["phases"] = _phase_states(compiled, state)
    before = _first_required_interaction(compiled, state["phase"], "before", state)
    if before:
        opened = _open_interaction(root, state, state["phase"], "before", before)
        return {**opened, "outcome_root": str(_record(root, state["run_id"]).parent)}
    _write(root, state)
    return {**state, "outcome_root": str(_record(root, state["run_id"]).parent)}


def compile_context(root: Path, config: Path, run_id: str | None) -> dict[str, Any]:
    state = _read(root, run_id) if run_id else status(root, None)
    if state.get("status") == "no_runs":
        raise ExecutionError("no local outcome exists; run `oci outcome begin` first")
    compiled = compile_workflow(config)
    phase = state["phase"]
    if phase not in compiled["instructions"]["phases"]:
        raise ExecutionError(f"workflow has no instructions for {phase}")
    runner, model = _policy(
        compiled, phase, state.get("interactive_session", {}).get("provider"), None
    )
    context_revision = f"filesystem:{compiled['workflow_revision']}"
    phase_contract: dict[str, Any] | None = None
    if phase == "intake":
        phase_contract = {
            "artifact": "intake/trajectory.json",
            "schema_version": "1",
            "ontology_revision_id": context_revision,
            "target": {
                "repository_id": f"local:{root.name}",
                "repository": root.name,
                "required": ["rationale", "candidates"],
            },
        }
    return {
        "schema_version": "outcomeci.interactive-context/v1alpha1",
        "run": state,
        "outcome_root": str(_record(root, state["run_id"]).parent),
        "runner": {"provider": runner, "model": model or "provider-default"},
        "context": compiled["context"],
        "phase_contract": phase_contract,
        "instructions": {
            "standup": compiled["instructions"]["orchestrator"],
            "phase": compiled["instructions"]["phases"][phase],
        },
        "workflow_revision": compiled["workflow_revision"],
    }


def validate_artifacts(root: Path, config: Path, run_id: str | None) -> dict[str, Any]:
    state = _read(root, run_id) if run_id else status(root, None)
    if state.get("status") == "no_runs":
        raise ExecutionError("no local outcome exists")
    compiled = compile_workflow(config)
    phase = state["phase"]
    repository = root.name
    outcome_root = _record(root, state["run_id"]).parent
    _validate_outputs(compiled, outcome_root, phase)
    standup = (outcome_root / "standup.md").read_text(encoding="utf-8")
    if "# Standup:" not in standup or "**Status**: active" not in standup:
        raise ExecutionError("agent did not produce a valid active Standup")
    context_revision = f"filesystem:{compiled['workflow_revision']}"
    if phase == "intake":
        try:
            trajectory = json.loads(
                (outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8")
            )
        except json.JSONDecodeError as exc:
            raise ExecutionError("intake trajectory is not valid JSON") from exc
        _validate_trajectory(
            trajectory, {"intent_context": {"ontology_revision_id": context_revision}}
        )
    session = state.get("interactive_session", {})
    runner, model = _policy(compiled, phase, session.get("provider"), None)
    transcript = _transcripts(
        runner,
        outcome_root,
        phase,
        session_id=session.get("session_id"),
        byte_offset=int(session.get("byte_offset") or 0),
        workspace=root,
        since=state.get("created_at"),
    )
    matches = _select_sessions(runner, session.get("session_id"), root, state.get("created_at"))
    if matches:
        session["session_id"] = session.get("session_id") or _session_details(matches[0], runner)[0]
        session["byte_offset"] = matches[0].stat().st_size
        state["interactive_session"] = session
    constitution = root / ".outcomeci" / "constitution.md"
    manifest = build_manifest(
        outcome_root=outcome_root,
        artifact_base=root,
        run_id=state["run_id"],
        workflow_run_id=None,
        trajectory_version=None,
        phase=phase,
        workflow_revision=compiled["workflow_revision"],
        backend_provider="filesystem",
        state_repository=None,
        context_provider="filesystem",
        context_revision_id=context_revision,
        constitution_sha256=hashlib.sha256(
            constitution.read_bytes() if constitution.exists() else b""
        ).hexdigest(),
        repository_base_commits={repository: _local_revision(root)},
        runner=runner,
        model=model,
        transcript=transcript,
        phase_contract=compiled["instructions"]["phases"][phase]["expects"],
    )
    (outcome_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    after = _first_required_interaction(compiled, phase, "after", state)
    if after:
        state.update(
            {"phase_output_ready": True, "workflow_revision": compiled["workflow_revision"]}
        )
        _open_interaction(root, state, phase, "after", after)
        return {
            "valid": True,
            "run_id": state["run_id"],
            "phase": phase,
            "status": state["status"],
            "interaction": state["pending_interaction"],
            "manifest": manifest,
        }
    completed = list(state.get("completed_phases", []))
    if phase not in completed:
        completed.append(phase)
    state.update(
        {
            "completed_phases": completed,
            "status": "ready_for_implementation" if phase == "tasks" else "awaiting_confirmation",
            "workflow_revision": compiled["workflow_revision"],
        }
    )
    state["ready_phases"] = _ready(compiled, completed)
    state["phases"] = _phase_states(compiled, state)
    _write(root, state)
    return {
        "valid": True,
        "run_id": state["run_id"],
        "phase": phase,
        "status": state["status"],
        "manifest": manifest,
    }


def advance(root: Path, config: Path, run_id: str | None, approve: bool) -> dict[str, Any]:
    state = _read(root, run_id) if run_id else status(root, None)
    if not approve:
        raise ExecutionError("advancing requires explicit --approve")
    if state.get("status") != "awaiting_confirmation":
        raise ExecutionError(f"outcome cannot advance from {state.get('status')}")
    compiled = compile_workflow(config)
    ready = _ready(compiled, state.get("completed_phases", []))
    if not ready:
        raise ExecutionError("outcome workflow is complete")
    state["phase"] = ready[0]
    state["status"] = "awaiting_agent"
    state["workflow_revision"] = compiled["workflow_revision"]
    state["ready_phases"] = ready
    state["phases"] = _phase_states(compiled, state)
    _write(root, state)
    return {**state, "outcome_root": str(_record(root, state["run_id"]).parent)}


def continue_run(
    root: Path,
    config: Path,
    run_id: str,
    approve: bool,
    *,
    agent: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    state = _read(root, run_id)
    if state.get("status") == "awaiting_input" and approve:
        pending = state.get("pending_interaction", {})
        state = respond(
            root,
            config,
            run_id,
            pending.get("id", ""),
            "Approved",
            approve=True,
            agent=agent,
            model=model,
        )
    if state.get("status") != "awaiting_confirmation":
        raise ExecutionError(f"outcome cannot continue from {state.get('status')}")
    if not approve:
        raise ExecutionError("continuation requires explicit --approve")
    compiled = compile_workflow(config)
    ready = _ready(compiled, state.get("completed_phases", []))
    if not ready:
        raise ExecutionError("outcome workflow is complete")
    state["phase"] = ready[0]
    state["status"] = "queued"
    _write(root, state)
    return _execute(root, config, state, agent=agent, model=model)


def retry(
    root: Path, config: Path, run_id: str, *, agent: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """Retry agent execution after a failure without replaying resolved gates."""
    state = _read(root, run_id)
    if state.get("status") != "error":
        raise ExecutionError(f"outcome cannot retry from {state.get('status')}")
    state["status"] = "queued"
    state.pop("error", None)
    _write(root, state)
    return _execute(root, config, state, agent=agent, model=model)


def _worker_live(outcome_root: Path) -> bool:
    try:
        worker = json.loads((outcome_root / "worker.json").read_text(encoding="utf-8"))
        if worker.get("status") == "queued" and not worker.get("pid"):
            started = datetime.fromisoformat(worker["started_at"])
            return (datetime.now(UTC) - started).total_seconds() < 30
        os.kill(int(worker["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def launch_worker(
    root: Path,
    config: Path,
    run_id: str,
    operation: str,
    *,
    interaction_id: str | None = None,
    message: str | None = None,
    approve: bool = False,
    reject: bool = False,
) -> dict[str, Any]:
    """Launch an outcome transition outside the Slack listener process tree."""
    outcome_root = _record(root, run_id).parent
    lock_path = outcome_root / "worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise ExecutionError("an outcome worker launch is already in progress") from exc
    if _worker_live(outcome_root):
        lock.close()
        raise ExecutionError("an outcome worker is already active")
    if operation == "respond" and (not interaction_id or message is None):
        raise ExecutionError("respond workers require an interaction and message")
    if operation not in {"continue", "retry", "respond"}:
        raise ExecutionError(f"unsupported worker operation: {operation}")
    worker_id = str(uuid.uuid4())
    argv = [
        sys.executable,
        "-m",
        "outcomeci.worker",
        operation,
        run_id,
        "--workspace",
        str(root),
        "--config",
        str(config),
        "--worker-id",
        worker_id,
    ]
    if interaction_id:
        argv.extend(["--interaction-id", interaction_id])
    if message is not None:
        argv.extend(["--message", message])
    if approve:
        argv.append("--approve")
    if reject:
        argv.append("--reject")
    log_path = outcome_root / "worker.log"
    worker_path = outcome_root / "worker.json"
    try:
        previous_worker = json.loads(worker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_worker = {}
    worker = {
        "schema_version": 1,
        "worker_id": worker_id,
        "attempt": int(previous_worker.get("attempt", 0)) + 1,
        "pid": None,
        "operation": operation,
        "status": "queued",
        "started_at": datetime.now(UTC).isoformat(),
        "log": str(log_path),
    }
    try:
        worker_path.write_text(
            json.dumps(worker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        log = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                argv,
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
        finally:
            log.close()
        worker["pid"] = process.pid
        worker_path.write_text(
            json.dumps(worker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    finally:
        lock.close()
    return {"status": "queued", "run_id": run_id, "worker": worker}


def recover(root: Path, config: Path, run_id: str) -> dict[str, Any]:
    state = _read(root, run_id)
    outcome_root = _record(root, run_id).parent
    if state.get("status") != "running":
        raise ExecutionError(f"outcome cannot recover from {state.get('status')}")
    if _worker_live(outcome_root):
        raise ExecutionError("outcome worker is still active")
    state.update(
        {"status": "error", "error": "previous outcome worker exited before recording completion"}
    )
    _write(root, state)
    return launch_worker(root, config, run_id, "retry")


def status(root: Path, run_id: str | None) -> dict[str, Any]:
    if run_id:
        return _read(root, run_id)
    records = sorted((root / ".outcomeci" / "outcomes").glob("*/run.json"), reverse=True)
    return _read(root, records[0].parent.name) if records else {"status": "no_runs"}


def request_input(root: Path, config: Path, run_id: str, interaction_id: str) -> dict[str, Any]:
    state = _read(root, run_id)
    compiled = compile_workflow(config)
    phase = state["phase"]
    definition = next(
        (
            item
            for item in compiled["instructions"]["phases"][phase]["humans"]["during"]
            if item["id"] == interaction_id
        ),
        None,
    )
    if definition is None:
        raise ExecutionError(f"phase {phase} has no during interaction {interaction_id}")
    if state.get("status") not in {"running", "awaiting_agent"}:
        raise ExecutionError(f"cannot request human input while outcome is {state.get('status')}")
    return _open_interaction(root, state, phase, "during", definition)


def respond(
    root: Path,
    config: Path,
    run_id: str,
    interaction_id: str,
    message: str,
    *,
    approve: bool = False,
    reject: bool = False,
    agent: str | None = None,
    model: str | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    state = _read(root, run_id)
    if approve and reject:
        raise ExecutionError("choose either --approve or --reject")
    pending = state.get("pending_interaction")
    if (
        state.get("status") != "awaiting_input"
        or not isinstance(pending, dict)
        or pending.get("id") != interaction_id
    ):
        raise ExecutionError(f"interaction {interaction_id} is not awaiting input")
    if not message.strip():
        raise ExecutionError("a response message is required")
    path = Path(pending["path"])
    request = json.loads(path.read_text(encoding="utf-8"))
    if request["interaction"] == "approval" and not (approve or reject):
        raise ExecutionError("approval interactions require --approve or --reject")
    request.update(
        {
            "status": "approved" if approve else "rejected" if reject else "answered",
            "response": {"message": message.strip(), "responded_at": datetime.now(UTC).isoformat()},
        }
    )
    path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    timing, phase = pending["timing"], pending["phase"]
    state.pop("pending_interaction", None)
    state.setdefault("interaction_history", []).append(
        {
            "phase": phase,
            "timing": timing,
            "id": interaction_id,
            "status": request["status"],
            "path": str(path),
        }
    )
    if reject or (timing == "after" and request["interaction"] == "review" and not approve):
        state.update({"status": "awaiting_agent", "phase_output_ready": False})
        _write(root, state)
        return state
    if timing == "after":
        completed = list(state.get("completed_phases", []))
        if phase not in completed:
            completed.append(phase)
        compiled = compile_workflow(config)
        next_interaction = _first_required_interaction(compiled, phase, "after", state)
        if next_interaction:
            return _open_interaction(root, state, phase, "after", next_interaction)
        state.update(
            {
                "completed_phases": completed,
                "phase_output_ready": False,
                "status": "ready_for_implementation"
                if phase == "tasks"
                else "awaiting_confirmation",
            }
        )
        state["ready_phases"] = _ready(compiled, completed)
        state["phases"] = _phase_states(compiled, state)
        _write(root, state)
        return state
    compiled = compile_workflow(config)
    if timing == "before":
        next_interaction = _first_required_interaction(compiled, phase, "before", state)
        if next_interaction:
            return _open_interaction(root, state, phase, "before", next_interaction)
    if not execute:
        state["status"] = "running"
        _write(root, state)
        return state
    state["status"] = "queued"
    _write(root, state)
    return _execute(root, config, state, agent=agent, model=model)
