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
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as validate_json

from . import templates
from .capability import serve as serve_capability
from .config import compile_workflow
from .contracts import FORMAT_CHECKER, ContractError, validate_trigger_payload
from .execution_events import event, safe_text
from .integrations import CredentialResolver, IntegrationExecutor
from .manifest import build_manifest
from .outcome import (
    _select_sessions,
    _session_details,
    _transcripts,
    _validate_trajectory,
)
from .process import ExecutionError, invoke
from .security import atomic_write_json


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
        name: (
            "completed"
            if name in completed
            else (
                "active"
                if name == state.get("phase") and state.get("status") == "running"
                else "queued"
                if name in ready
                else "blocked"
            )
        )
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


def _call_succeeded(call: dict[str, Any]) -> bool:
    """A broker journal call only truly succeeded if the broker itself
    reported ok AND the provider's own nested result (when present) didn't
    override that with an explicit ok: false. Shared by
    _validate_required_effects (must-confirm gating) and
    _write_effect_receipts (the reported artifact) so they can't drift on
    what "confirmed" means."""
    result = call.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False
    provider_result = (
        result.get("output", {}).get("result", {}) if isinstance(result.get("output"), dict) else {}
    )
    return not (isinstance(provider_result, dict) and provider_result.get("ok") is False)


def _validate_required_effects(
    root: Path, compiled: dict[str, Any], run_id: str, phase: str
) -> None:
    """Require broker-confirmed evidence for effects declared as mandatory."""
    required = compiled["instructions"]["phases"][phase].get("required_capabilities", [])
    if not required:
        return
    journal = root / ".outcomeci" / ".broker" / run_id / "journal.json"
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError("required integration effect evidence is missing") from exc
    calls = state.get("calls", {})
    if not isinstance(calls, dict):
        raise ExecutionError("required integration effect evidence is invalid")
    confirmed: set[str] = set()
    for call in calls.values():
        if not isinstance(call, dict) or call.get("status") != "confirmed":
            continue
        if not _call_succeeded(call):
            continue
        capability = call.get("capability")
        if isinstance(capability, str):
            confirmed.add(capability)
    missing = sorted(set(required) - confirmed)
    if missing:
        raise ExecutionError("required integration effect was not confirmed: " + ", ".join(missing))


def _write_effect_receipts(root: Path, outcome_root: Path, run_id: str, phase: str) -> Path:
    """Materialize credential-free effect evidence for output validation and repair."""
    journal = root / ".outcomeci" / ".broker" / run_id / "journal.json"
    calls: list[dict[str, Any]] = []
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    for receipt in (state.get("calls") or {}).values():
        if not isinstance(receipt, dict):
            continue
        result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
        calls.append(
            {
                "capability": receipt.get("capability"),
                "proposal_sha256": receipt.get("proposal_sha256"),
                "status": receipt.get("status"),
                "ok": _call_succeeded(receipt),
                "http_status": (
                    result.get("status") if isinstance(result.get("status"), int) else None
                ),
            }
        )
    target = outcome_root / "effects.json"
    target.write_text(
        json.dumps(
            {"schema_version": "1", "phase": phase, "effects": calls},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _repair_outputs(
    root: Path,
    compiled: dict[str, Any],
    outcome_root: Path,
    phase: str,
    runner: str,
    model: str | None,
    error: ExecutionError,
    writable_artifacts: list[Path],
    excluded_env: set[str],
    *,
    container_isolated: bool,
) -> str:
    """Run one output-only repair with no capability socket or provider credentials."""
    contracts = compiled["instructions"]["phases"][phase]["expects"]["outputs"]
    prompt = (
        "Repair the declared workflow output artifacts only. External effects may already "
        "have completed and must not be repeated. You have no integration or human "
        "capabilities. Read effects.json for sanitized effect evidence, then edit only the "
        "declared output paths so they satisfy their contracts. Do not modify source code.\n"
        f"Outcome directory: {outcome_root}\n"
        f"Validation error: {safe_text(str(error))}\n"
        f"Output contracts: {json.dumps(contracts, separators=(',', ':'))}"
    )
    return invoke(
        runner,
        model,
        prompt,
        root,
        600,
        allow_local_auth=True,
        extra_env={},
        writable_paths=writable_artifacts,
        excluded_env=excluded_env,
        container_isolated=container_isolated,
    )


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


def _finish_interaction(
    root: Path,
    state: dict[str, Any],
    phase: str,
    timing: str,
    definition: dict[str, Any],
    *,
    status: str,
    message: str,
) -> None:
    """Write an interaction as already-resolved, without ever passing through
    the durable awaiting_input pause -- used by delivery types (currently
    only 'reaction') that the runtime itself resolves synchronously."""
    request = {
        "schema_version": 1,
        "run_id": state["run_id"],
        "phase": phase,
        "timing": timing,
        "status": status,
        "requested_at": datetime.now(UTC).isoformat(),
        **definition,
        "response": {"message": message, "responded_at": datetime.now(UTC).isoformat()},
    }
    path = _interaction_path(root, state["run_id"], phase, definition["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state.setdefault("interaction_history", []).append(
        {
            "phase": phase,
            "timing": timing,
            "id": definition["id"],
            "status": status,
            "path": str(path),
        }
    )


def _provider_value(root: Path, run_id: str, integration: str, value: str) -> str:
    if not value.startswith("ref:"):
        return value
    journal = root / ".outcomeci" / ".broker" / run_id / "journal.json"
    try:
        state = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    # The broker keeps one reference table per integration.
    resolved = state.get("references", {}).get(integration, {}).get(value)
    if not isinstance(resolved, str) or not resolved:
        raise ExecutionError(f"reaction delivery source holds an unresolvable reference: {value}")
    return resolved


def _resolve_reaction(
    root: Path,
    config: Path,
    state: dict[str, Any],
    phase: str,
    timing: str,
    definition: dict[str, Any],
    credential_resolver: CredentialResolver | None,
) -> None:
    """Poll Slack for the configured reaction on a prior phase's message,
    blocking the current call for up to wait.timeout_seconds. Runtime-driven,
    not agent-driven -- IntegrationExecutor(reviewed=True) bypasses the
    independent policy-review agent deliberately: this is a fixed,
    non-agent-controllable action (check this exact message's reactions),
    not an arbitrary agent-initiated request."""
    if credential_resolver is None:
        raise ExecutionError("reaction delivery requires a credential resolver")
    delivery = definition["delivery"]
    compiled = compile_workflow(config)
    producer, _, output_name = delivery["source"].partition(".outputs.")
    try:
        output = next(
            item
            for item in compiled["instructions"]["phases"][producer]["expects"]["outputs"]
            if item["name"] == output_name
        )
    except (KeyError, StopIteration) as exc:
        raise ExecutionError(
            f"reaction delivery source is unresolvable: {delivery['source']}"
        ) from exc
    source_file = root / ".outcomeci" / "outcomes" / state["run_id"] / output["path"]
    try:
        value = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(
            f"reaction delivery source could not be read: {output['path']}"
        ) from exc
    channel, ts = value.get("channel"), value.get("ts")
    if not isinstance(channel, str) or not isinstance(ts, str):
        raise ExecutionError(f"reaction delivery source is missing channel/ts: {output['path']}")
    # With access.opaque_identifiers the producing agent only ever saw ref:
    # tokens, so that is what it wrote. The broker journal holds the real values.
    channel = _provider_value(root, state["run_id"], "slack", channel)
    ts = _provider_value(root, state["run_id"], "slack", ts)
    executor = IntegrationExecutor(compiled, resolver=credential_resolver, reviewed=True)
    emoji = delivery["emoji"]
    deadline = time.monotonic() + definition["wait"]["timeout_seconds"]
    while True:
        result = executor.execute(
            "slack.get_reactions", {"channel": channel, "timestamp": ts}, phase=phase
        )
        reactions = (result.get("output") or {}).get("reactions") or []
        if any(
            isinstance(item, dict) and item.get("name") == emoji and item.get("count", 0) >= 1
            for item in reactions
        ):
            _finish_interaction(
                root,
                state,
                phase,
                timing,
                definition,
                status="approved",
                message=f"Approved via Slack :{emoji}: reaction",
            )
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(delivery["poll_interval_seconds"])
    if definition.get("on_timeout") == "continue":
        _finish_interaction(
            root,
            state,
            phase,
            timing,
            definition,
            status="answered",
            message="Approval window expired",
        )
        return
    raise ExecutionError(f"approval window expired for interaction {definition['id']}")


def _open_interaction(
    root: Path,
    state: dict[str, Any],
    phase: str,
    timing: str,
    definition: dict[str, Any],
    *,
    config: Path | None = None,
    credential_resolver: CredentialResolver | None = None,
) -> dict[str, Any] | None:
    if definition.get("delivery", {}).get("type") == "reaction":
        _resolve_reaction(root, config, state, phase, timing, definition, credential_resolver)
        return None
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
    compiled: dict[str, Any],
    phase: str,
    timing: str,
    state: dict[str, Any] | None = None,
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
    state["updated_at"] = datetime.now(UTC).isoformat()
    atomic_write_json(path, state)


def _local_revision(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
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
    credential_resolver: CredentialResolver | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    policy_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    execution_backend: str = "filesystem",
    _container_isolated: bool = False,
) -> dict[str, Any]:
    compiled = compile_workflow(config)
    configured_backend = compiled["workflow"]["spec"]["backend"].get("provider")
    if execution_backend not in {"filesystem", "outcomeci"}:
        raise ExecutionError("unsupported execution backend")
    if configured_backend != execution_backend:
        raise ExecutionError(
            f"{execution_backend} execution requires spec.backend.provider: {execution_backend}"
        )
    if execution_backend == "outcomeci" and credential_resolver is None:
        raise ExecutionError("OutcomeCI execution requires a scoped credential resolver")
    if _container_isolated and execution_backend != "outcomeci":
        raise ExecutionError("container isolation is reserved for OutcomeCI execution")
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
    context_revision = f"{execution_backend}:{compiled['workflow_revision']}"
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
        "required_capabilities": compiled["instructions"]["phases"][phase].get(
            "required_capabilities", []
        ),
        "human_context": _human_context(state, outcome_root),
    }
    intake_contract = ""
    if phase == "intake":
        intake_contract = f"""
Write intake/trajectory.json with schema_version \"1\", ontology_revision_id
\"{context_revision}\", and at least one target. The local target must use
repository_id \"local:{repository}\", repository \"{repository}\", a non-empty
rationale, and a candidates array following the stable role and disposition
contract above. Use paths relative to this repository.
"""
    environment = (
        f"This is an OutcomeCI Cloud execution in an isolated workspace at {root}. "
        "Use only the supplied workflow context and scoped capabilities."
        if execution_backend == "outcomeci"
        else f"This is a filesystem-backed local Standup. Work in {root}. "
        "There is no OutcomeCI Cloud or Digital Twin; inspect the local repository directly."
    )
    prompt = templates.EXECUTION_TASK.format(
        shared=shared,
        instructions=instructions,
        environment=environment,
        outcome_root=outcome_root,
        phase=phase,
        run_id=state["run_id"],
        root=root,
        intake_contract=intake_contract,
        context_json=json.dumps(context, separators=(",", ":")),
    )
    runtime_cli = shlex.join([sys.executable, "-m", "outcomeci.cli"])
    prompt += templates.EXECUTION_CLI_ADDENDUM.format(runtime_cli=runtime_cli, phase=phase)
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
            root,
            config,
            state["run_id"],
            phase,
            compiled=compiled,
            resolver=credential_resolver,
            event_sink=event_sink,
            policy_reviewer=policy_reviewer,
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
                container_isolated=_container_isolated,
            )
        persisted = _read(root, state["run_id"])
        if persisted.get("status") == "awaiting_input":
            return persisted
        _write_effect_receipts(root, outcome_root, state["run_id"], phase)
        try:
            try:
                _validate_outputs(compiled, outcome_root, phase)
            except ExecutionError as validation_error:
                if event_sink:
                    event_sink(
                        event(
                            "artifact.repair_started",
                            phase,
                            "workflow.outputs",
                            "Output contract repair started",
                            reason=safe_text(str(validation_error)),
                            level="warning",
                        )
                    )
                try:
                    repair_summary = _repair_outputs(
                        root,
                        compiled,
                        outcome_root,
                        phase,
                        runner,
                        chosen_model,
                        validation_error,
                        writable_artifacts,
                        connection_secrets,
                        container_isolated=_container_isolated,
                    )
                    _validate_outputs(compiled, outcome_root, phase)
                except ExecutionError as repair_error:
                    if event_sink:
                        event_sink(
                            event(
                                "artifact.repair_failed",
                                phase,
                                "workflow.outputs",
                                "Output contract repair failed",
                                reason=safe_text(str(repair_error)),
                                level="error",
                            )
                        )
                    raise
                if event_sink:
                    event_sink(
                        event(
                            "artifact.repair_completed",
                            phase,
                            "workflow.outputs",
                            "Output contract repair completed",
                        )
                    )
                summary = f"{summary}\n{repair_summary}"
            _validate_required_effects(root, compiled, state["run_id"], phase)
            if phase == "intake":
                trajectory = json.loads(
                    (outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8")
                )
                _validate_trajectory(
                    trajectory,
                    {"intent_context": {"ontology_revision_id": context_revision}},
                )
        finally:
            # Copy the agent's session transcript in regardless of whether the
            # validation above succeeded -- a phase that fails required-effects
            # or output validation is exactly the case someone needs to inspect
            # what the agent actually did, and this used to run only on the
            # success path, leaving failed runs with no transcript at all.
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
        backend_provider=execution_backend,
        state_repository=None,
        context_provider=compiled["workflow"]["spec"]["context"].get("provider", "filesystem"),
        context_revision_id=context_revision,
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
    root: Path,
    config: Path,
    intent: str,
    *,
    agent: str | None = None,
    model: str | None = None,
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
        opened = _open_interaction(root, state, first, "before", before, config=config)
        if opened is not None:
            return opened
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
    credential_resolver: CredentialResolver | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    policy_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    execution_backend: str = "filesystem",
    _container_isolated: bool = False,
) -> dict[str, Any]:
    """Validate and materialize a named trigger before any agent execution."""
    compiled = compile_workflow(config)
    definition = compiled["triggers"].get(trigger_name)
    if definition is None:
        raise ExecutionError(f"workflow does not declare trigger {trigger_name}")
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    limit = 2 * 1024 * 1024 if definition["type"] == "webhook.received" else 1024 * 1024
    if len(encoded) > limit:
        raise ExecutionError(
            f"trigger payload exceeds the {limit // (1024 * 1024)} MiB local limit"
        )
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
        opened = _open_interaction(
            root,
            state,
            first,
            "before",
            before,
            config=config,
            credential_resolver=credential_resolver,
        )
        if opened is not None:
            return opened
    return _execute(
        root,
        config,
        state,
        agent=agent,
        model=model,
        credential_resolver=credential_resolver,
        event_sink=event_sink,
        policy_reviewer=policy_reviewer,
        execution_backend=execution_backend,
        _container_isolated=_container_isolated,
    )


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
        opened = _open_interaction(root, state, state["phase"], "before", before, config=config)
        if opened is not None:
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
            {
                "phase_output_ready": True,
                "workflow_revision": compiled["workflow_revision"],
            }
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
            "status": ("ready_for_implementation" if phase == "tasks" else "awaiting_confirmation"),
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
    credential_resolver: CredentialResolver | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    policy_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    execution_backend: str = "filesystem",
    _container_isolated: bool = False,
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
            credential_resolver=credential_resolver,
            event_sink=event_sink,
            policy_reviewer=policy_reviewer,
            execution_backend=execution_backend,
            _container_isolated=_container_isolated,
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
    before = _first_required_interaction(compiled, state["phase"], "before", state)
    if before:
        opened = _open_interaction(
            root,
            state,
            state["phase"],
            "before",
            before,
            config=config,
            credential_resolver=credential_resolver,
        )
        if opened is not None:
            return opened
    return _execute(
        root,
        config,
        state,
        agent=agent,
        model=model,
        credential_resolver=credential_resolver,
        event_sink=event_sink,
        policy_reviewer=policy_reviewer,
        execution_backend=execution_backend,
        _container_isolated=_container_isolated,
    )


def retry(
    root: Path,
    config: Path,
    run_id: str,
    *,
    agent: str | None = None,
    model: str | None = None,
    credential_resolver: CredentialResolver | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    policy_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    execution_backend: str = "filesystem",
    _container_isolated: bool = False,
) -> dict[str, Any]:
    """Retry agent execution after a failure without replaying resolved gates."""
    state = _read(root, run_id)
    if state.get("status") != "error":
        raise ExecutionError(f"outcome cannot retry from {state.get('status')}")
    state["status"] = "queued"
    state.pop("error", None)
    _write(root, state)
    return _execute(
        root,
        config,
        state,
        agent=agent,
        model=model,
        credential_resolver=credential_resolver,
        event_sink=event_sink,
        policy_reviewer=policy_reviewer,
        execution_backend=execution_backend,
        _container_isolated=_container_isolated,
    )


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
        {
            "status": "error",
            "error": "previous outcome worker exited before recording completion",
        }
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
    credential_resolver: CredentialResolver | None = None,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    policy_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    execution_backend: str = "filesystem",
    _container_isolated: bool = False,
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
                "status": (
                    "ready_for_implementation" if phase == "tasks" else "awaiting_confirmation"
                ),
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
            opened = _open_interaction(
                root,
                state,
                phase,
                "before",
                next_interaction,
                config=config,
                credential_resolver=credential_resolver,
            )
            if opened is not None:
                return opened
    if not execute:
        state["status"] = "running"
        _write(root, state)
        return state
    state["status"] = "queued"
    _write(root, state)
    return _execute(
        root,
        config,
        state,
        agent=agent,
        model=model,
        credential_resolver=credential_resolver,
        event_sink=event_sink,
        policy_reviewer=policy_reviewer,
        execution_backend=execution_backend,
        _container_isolated=_container_isolated,
    )
