"""Filesystem-backed execution of an OutcomeWorkflow with a local agent."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import compile_workflow
from .manifest import build_manifest
from .outcome import _expected, _select_sessions, _session_details, _transcripts, _validate_trajectory
from .process import ExecutionError, invoke

PHASES = ("intake", "plan", "tasks")


def _id(intent: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", intent.casefold()).strip("-")[:36] or "outcome"
    return f"{stamp}-{slug}"


def _policy(compiled: dict[str, Any], phase: str, agent: str | None, model: str | None) -> tuple[str, str | None]:
    agents = compiled["workflow"]["spec"].get("agents", {})
    default = agents.get("default", {})
    selected = agents.get("phases", {}).get(phase, {})
    runner = agent or selected.get("runner") or default.get("runner")
    chosen_model = model or selected.get("model") or default.get("model")
    if runner not in {"codex", "claude"}:
        raise ExecutionError(f"no supported agent configured for {phase}")
    return runner, chosen_model


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
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
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
        provider = compiled["workflow"]["spec"].get("agents", {}).get("default", {}).get("runner", "codex")
        session_id = None
    matches = _select_sessions(provider, session_id, root, None)
    if matches and not session_id:
        session_id = _session_details(matches[0], provider)[0]
    return {
        "provider": provider,
        "session_id": session_id,
        "byte_offset": matches[0].stat().st_size if matches else 0,
    }


def _execute(root: Path, config: Path, state: dict[str, Any], *, agent: str | None = None, model: str | None = None) -> dict[str, Any]:
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
        "run_id": state["run_id"], "phase": phase, "intent": state["intent"],
        "repository": {"name": repository, "checkout": str(root)},
        "prior_phases": state.get("completed_phases", []),
        "workflow_revision": compiled["workflow_revision"],
        "context_files": compiled["context"]["files"],
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
    prompt = f"{shared}\n\n{instructions}\n\nThis is a filesystem-backed local Standup. Work in {root}. Write durable artifacts beneath {outcome_root}. During intake, plan, and tasks, do not modify product source files. There is no OutcomeCI Cloud or Digital Twin; inspect the local repository directly.\n{intake_contract}\n{json.dumps(context, separators=(',', ':'))}"
    state.update({"status": "running", "agent": runner, "model": chosen_model, "workflow_revision": compiled["workflow_revision"]})
    _write(root, state)
    try:
        summary = invoke(runner, chosen_model, prompt, root, 7200)
        expected = _expected(outcome_root, [repository], phase)
        if any(not path.is_file() or not path.read_text(encoding="utf-8").strip() for path in expected):
            raise ExecutionError("agent did not produce the complete local outcome artifact set")
        if phase == "intake":
            trajectory = json.loads((outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8"))
            _validate_trajectory(trajectory, {"intent_context": {"ontology_revision_id": local_revision}})
        transcripts = _transcripts(runner, outcome_root, phase)
    except (ExecutionError, OSError, json.JSONDecodeError) as exc:
        state.update({"status": "error", "error": str(exc)})
        _write(root, state)
        if isinstance(exc, ExecutionError):
            raise
        raise ExecutionError(f"invalid local outcome artifacts: {exc}") from exc
    state["completed_phases"] = [*state.get("completed_phases", []), phase]
    state["status"] = "awaiting_confirmation" if phase != "tasks" else "ready_for_implementation"
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
    )
    (outcome_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write(root, state)
    return state


def start(root: Path, config: Path, intent: str, *, agent: str | None = None, model: str | None = None) -> dict[str, Any]:
    if not intent.strip():
        raise ExecutionError("intent is required")
    state = {"schema_version": 1, "run_id": _id(intent), "intent": intent.strip(), "phase": "intake", "status": "queued", "completed_phases": [], "created_at": datetime.now(timezone.utc).isoformat()}
    return _execute(root, config, state, agent=agent, model=model)


def begin(root: Path, config: Path, intent: str) -> dict[str, Any]:
    """Create an interactive run without launching a child agent."""
    if not intent.strip():
        raise ExecutionError("intent is required")
    compiled = compile_workflow(config)
    if compiled["workflow"]["spec"]["backend"].get("provider") != "filesystem":
        raise ExecutionError("interactive local execution requires spec.backend.provider: filesystem")
    session = _interactive_session(root, compiled)
    state = {
        "schema_version": 1,
        "run_id": _id(intent),
        "intent": intent.strip(),
        "phase": "intake",
        "status": "awaiting_agent",
        "completed_phases": [],
        "workflow_revision": compiled["workflow_revision"],
        "interactive_session": session,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
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
    runner, model = _policy(compiled, phase, state.get("interactive_session", {}).get("provider"), None)
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
    expected = _expected(outcome_root, [repository], phase)
    missing = [str(path.relative_to(root)) for path in expected if not path.is_file() or not path.read_text(encoding="utf-8").strip()]
    if missing:
        raise ExecutionError(f"incomplete {phase} artifacts: {', '.join(missing)}")
    standup = (outcome_root / "standup.md").read_text(encoding="utf-8")
    if "# Standup:" not in standup or "**Status**: active" not in standup:
        raise ExecutionError("agent did not produce a valid active Standup")
    context_revision = f"filesystem:{compiled['workflow_revision']}"
    if phase == "intake":
        try:
            trajectory = json.loads((outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ExecutionError("intake trajectory is not valid JSON") from exc
        _validate_trajectory(trajectory, {"intent_context": {"ontology_revision_id": context_revision}})
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
        outcome_root=outcome_root, artifact_base=root, run_id=state["run_id"],
        workflow_run_id=None, trajectory_version=None, phase=phase,
        workflow_revision=compiled["workflow_revision"], backend_provider="filesystem",
        state_repository=None, context_provider="filesystem", context_revision_id=context_revision,
        constitution_sha256=hashlib.sha256(constitution.read_bytes()).hexdigest(),
        repository_base_commits={repository: _local_revision(root)}, runner=runner,
        model=model, transcript=transcript,
    )
    (outcome_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    completed = list(state.get("completed_phases", []))
    if phase not in completed:
        completed.append(phase)
    state.update({
        "completed_phases": completed,
        "status": "ready_for_implementation" if phase == "tasks" else "awaiting_confirmation",
        "workflow_revision": compiled["workflow_revision"],
    })
    _write(root, state)
    return {"valid": True, "run_id": state["run_id"], "phase": phase, "status": state["status"], "manifest": manifest}


def advance(root: Path, config: Path, run_id: str | None, approve: bool) -> dict[str, Any]:
    state = _read(root, run_id) if run_id else status(root, None)
    if not approve:
        raise ExecutionError("advancing requires explicit --approve")
    if state.get("status") != "awaiting_confirmation":
        raise ExecutionError(f"outcome cannot advance from {state.get('status')}")
    state["phase"] = PHASES[PHASES.index(state["phase"]) + 1]
    state["status"] = "awaiting_agent"
    state["workflow_revision"] = compile_workflow(config)["workflow_revision"]
    _write(root, state)
    return {**state, "outcome_root": str(_record(root, state["run_id"]).parent)}


def continue_run(root: Path, config: Path, run_id: str, approve: bool, *, agent: str | None = None, model: str | None = None) -> dict[str, Any]:
    state = _read(root, run_id)
    if state.get("status") != "awaiting_confirmation":
        raise ExecutionError(f"outcome cannot continue from {state.get('status')}")
    if not approve:
        raise ExecutionError("continuation requires explicit --approve")
    current = state["phase"]
    state["phase"] = PHASES[PHASES.index(current) + 1]
    state["status"] = "queued"
    _write(root, state)
    return _execute(root, config, state, agent=agent, model=model)


def status(root: Path, run_id: str | None) -> dict[str, Any]:
    if run_id:
        return _read(root, run_id)
    records = sorted((root / ".outcomeci" / "outcomes").glob("*/run.json"), reverse=True)
    return _read(root, records[0].parent.name) if records else {"status": "no_runs"}
