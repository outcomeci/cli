"""Filesystem-backed execution of an OutcomeWorkflow with a local agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as validate_json

from .config import compile_workflow
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
    if runner not in {"codex", "claude"}:
        raise ExecutionError(f"no supported agent configured for {phase}")
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
                        value, compiled["instructions"]["schemas"][contract["schema"]]["value"]
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
    compiled: dict[str, Any], outcome_root: Path, phase: str, intent: str
) -> list[dict[str, Any]]:
    values = []
    for contract in compiled["instructions"]["phases"][phase]["expects"]["inputs"]:
        source = contract["from"]
        value: dict[str, Any] = {**contract}
        if source == "runtime.intent":
            value["value"] = intent
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
        values.append(value)
    return values


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
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    repository = root.name
    shared = compiled["instructions"]["standup"]["content"]
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
        "inputs": _input_context(compiled, outcome_root, phase, state["intent"]),
        "outputs": compiled["instructions"]["phases"][phase]["expects"]["outputs"],
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
    prompt = f"{shared}\n\n{instructions}\n\nThis is a filesystem-backed local Standup. Work in {root}. Write durable artifacts beneath {outcome_root}. During intake, plan, and tasks, do not modify product source files. There is no OutcomeCI Cloud or Digital Twin; inspect the local repository directly. Human interactions available during this phase are included in the phase contract. If one is needed, run `oci outcome request-input <interaction-id> --run {state['run_id']} --workspace {root}` and stop so the requester can respond.\n{intake_contract}\n{json.dumps(context, separators=(',', ':'))}"
    state.update(
        {
            "status": "running",
            "agent": runner,
            "model": chosen_model,
            "workflow_revision": compiled["workflow_revision"],
        }
    )
    _write(root, state)
    try:
        summary = invoke(runner, chosen_model, prompt, root, 7200)
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
        transcripts = _transcripts(runner, outcome_root, phase)
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
        constitution_sha256=hashlib.sha256(constitution.read_bytes()).hexdigest(),
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


def begin(root: Path, config: Path, intent: str) -> dict[str, Any]:
    """Create an interactive run without launching a child agent."""
    if not intent.strip():
        raise ExecutionError("intent is required")
    compiled = compile_workflow(config)
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
            "standup": compiled["instructions"]["standup"],
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
        constitution_sha256=hashlib.sha256(constitution.read_bytes()).hexdigest(),
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
            "response": {
                "message": message.strip(),
                "responded_at": datetime.now(UTC).isoformat(),
            },
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
    state["status"] = "queued"
    _write(root, state)
    return _execute(root, config, state, agent=agent, model=model)
