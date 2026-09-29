"""Execution of an outcomeci.workflow/v1 run, one step at a time."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
from .process import ExecutionError, invoke
from .security import atomic_write_json
from .transcripts import _transcripts


@dataclass
class ExecutionOptions:
    """The agent and execution context every entry point that can reach
    _execute() forwards: trigger(), continue_run() and retry()."""

    agent: str | None = None
    model: str | None = None
    credential_resolver: CredentialResolver | None = None
    event_sink: Callable[[dict[str, Any]], None] | None = None
    policy_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    _container_isolated: bool = False


_DEFAULT_EXECUTION_OPTIONS = ExecutionOptions()


def _id(intent: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    slug = re.sub(r"[^a-z0-9]+", "-", intent.casefold()).strip("-")[:36] or "outcome"
    return f"{stamp}-{slug}"


def _policy(
    compiled: dict[str, Any], phase: str, agent: str | None, model: str | None
) -> tuple[str, str | None]:
    selected = compiled["instructions"]["steps"].get(phase, {}).get("policy", {})
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
        for name, step in compiled["instructions"]["steps"].items()
        if name not in done and set(step["needs"]) <= done
    )


def _step_states(compiled: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    completed = set(state.get("completed_steps", []))
    skipped = set(state.get("skipped_steps", []))
    ready = set(_ready(compiled, list(completed)))
    return {
        name: (
            "skipped"
            if name in skipped
            else "completed"
            if name in completed
            else (
                "active"
                if name == state.get("step") and state.get("status") == "running"
                else "queued"
                if name in ready
                else "blocked"
            )
        )
        for name in compiled["instructions"]["steps"]
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
    paths = []
    for contract in compiled["instructions"]["steps"][phase]["expects"]["outputs"]:
        path = _artifact_path(outcome_root, contract)
        if contract["media_type"] == "inode/directory":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        paths.append(path)
    return paths


def _validate_outputs(compiled: dict[str, Any], outcome_root: Path, phase: str) -> None:
    for contract in compiled["instructions"]["steps"][phase]["expects"]["outputs"]:
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
    required = compiled["instructions"]["steps"][phase].get("required_capabilities", [])
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
    contracts = compiled["instructions"]["steps"][phase]["expects"]["outputs"]
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
    """Record an await or converse interaction the runtime resolved itself."""
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
            "step": phase,
            "timing": timing,
            "id": definition["id"],
            "status": status,
            "path": str(path),
        }
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


def _connection_secrets(compiled: dict[str, Any]) -> set[str]:
    """Environment variables holding connection credentials, kept from every agent."""
    return {
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


def _execute(
    root: Path,
    config: Path,
    state: dict[str, Any],
    *,
    options: ExecutionOptions = _DEFAULT_EXECUTION_OPTIONS,
) -> dict[str, Any]:
    agent = options.agent
    model = options.model
    credential_resolver = options.credential_resolver
    compiled = compile_workflow(config)
    if credential_resolver is None:
        raise ExecutionError("OutcomeCI execution requires a scoped credential resolver")
    phase = state["step"]
    step_block = compiled["instructions"]["steps"][phase].get("v1")
    if step_block is None:
        raise ExecutionError(f"step {phase} is not an outcomeci.workflow/v1 step")
    if step_block["kind"] != "agent":
        raise ExecutionError(f"step {phase} is driven by the runtime, not an agent")
    runner, chosen_model = _policy(compiled, phase, agent, model)
    outcome_root = root / ".outcomeci" / "outcomes" / state["run_id"]
    writable_artifacts = _prepare_writable_artifacts(compiled, outcome_root, phase)
    connection_secrets = _connection_secrets(compiled)
    context_revision = f"outcomeci:{compiled['workflow_revision']}"
    environment = (
        f"This is an OutcomeCI execution in an isolated workspace at {root}. "
        "Use only the supplied workflow context and scoped capabilities."
    )
    runtime_cli = shlex.join([sys.executable, "-m", "outcomeci.cli"])
    try:
        invocations = _step_invocations(
            root,
            compiled,
            state,
            phase,
            step_block,
            outcome_root=outcome_root,
            environment=environment,
            runtime_cli=runtime_cli,
            writable_artifacts=writable_artifacts,
        )
    except ExecutionError as exc:
        state.update({"status": "error", "error": str(exc)})
        state["steps"] = _step_states(compiled, state)
        _write(root, state)
        raise
    state.update(
        {
            "status": "running",
            "agent": runner,
            "model": chosen_model,
            "workflow_revision": compiled["workflow_revision"],
        }
    )
    _write(root, state)
    return _run_phase(
        root,
        config,
        compiled,
        state,
        phase,
        invocations,
        options=options,
        runner=runner,
        chosen_model=chosen_model,
        outcome_root=outcome_root,
        writable_artifacts=writable_artifacts,
        connection_secrets=connection_secrets,
        context_revision=context_revision,
        step_block=step_block,
    )


def _step_invocations(
    root: Path,
    compiled: dict[str, Any],
    state: dict[str, Any],
    phase: str,
    step_block: dict[str, Any],
    *,
    outcome_root: Path,
    environment: str,
    runtime_cli: str,
    writable_artifacts: list[Path],
) -> list[tuple[str, dict[str, Any]]]:
    """One agent run per for_each item (one run otherwise), each with its grants.

    Grants resolve here, from the run's records before any of the step's agents
    start, so nothing an item's agent writes can widen a later item's grants."""
    from . import v1_runtime

    invocations = []
    for index, bound in enumerate(v1_runtime.items(root, state, step_block)):
        result_path = None
        if "for_each" in step_block and step_block.get("returns"):
            result_path = v1_runtime.item_path(root, state["run_id"], step_block, index)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.touch(exist_ok=True)
            writable_artifacts.append(result_path)
        grants = v1_runtime.resolve_grants(root, state, step_block, bound)
        prompt = _step_prompt(
            root,
            compiled,
            state,
            phase,
            step_block,
            outcome_root=outcome_root,
            environment=environment,
            runtime_cli=runtime_cli,
            bound=bound,
            result_path=result_path,
            grants=grants,
        )
        invocations.append(
            (
                prompt,
                {
                    "bound": bound,
                    "grants": grants,
                    "inputs": v1_runtime.inputs(root, state, step_block, bound),
                },
            )
        )
    return invocations


def _step_prompt(
    root: Path,
    compiled: dict[str, Any],
    state: dict[str, Any],
    phase: str,
    step_block: dict[str, Any],
    *,
    outcome_root: Path,
    environment: str,
    runtime_cli: str,
    bound: dict[str, Any] | None = None,
    result_path: Path | None = None,
    grants: list[dict[str, Any]] | None = None,
) -> str:
    """The prompt for one outcomeci.workflow/v1 step: its inputs, grants and result shape.

    A for_each step gets one prompt per item, with the item bound to its name
    and its own result path."""
    from . import v1_runtime

    describer = IntegrationExecutor(compiled)
    returns = step_block.get("returns")
    if returns and result_path is not None:
        returns = {"path": result_path, "schema": returns["item"]["schema"]}
    elif returns:
        returns = {"path": outcome_root / returns["path"], "schema": returns["schema"]}
    context = {
        "run_id": state["run_id"],
        "step": phase,
        "workflow_revision": compiled["workflow_revision"],
        "inputs": v1_runtime.inputs(root, state, step_block, bound),
        "capabilities": [
            describer.describe(name)
            for name in compiled["instructions"]["steps"][phase].get("capabilities", [])
        ],
        "grants": grants,
        "policy": step_block.get("policy"),
        "returns": (
            {"path": str(returns["path"]), "schema": returns["schema"]} if returns else None
        ),
    }
    return templates.V1_STEP_TASK.format(
        shared=compiled["instructions"]["orchestrator"]["content"],
        instructions=compiled["instructions"]["steps"][phase]["content"],
        environment=environment,
        outcome_root=outcome_root,
        phase=phase,
        runtime_cli=runtime_cli,
        context_json=json.dumps(context, separators=(",", ":")),
    )


def _run_phase(
    root: Path,
    config: Path,
    compiled: dict[str, Any],
    state: dict[str, Any],
    phase: str,
    invocations: list[tuple[str, dict[str, Any] | None]],
    *,
    options: ExecutionOptions,
    runner: str,
    chosen_model: str | None,
    outcome_root: Path,
    writable_artifacts: list[Path],
    connection_secrets: set[str],
    context_revision: str,
    step_block: dict[str, Any],
) -> dict[str, Any]:
    credential_resolver = options.credential_resolver
    event_sink = options.event_sink
    policy_reviewer = options.policy_reviewer
    _container_isolated = options._container_isolated
    repository = root.name
    phase_started_at = datetime.now(UTC).isoformat()
    try:
        summaries = []
        for prompt, scope in invocations:
            with serve_capability(
                root,
                config,
                state["run_id"],
                phase,
                compiled=compiled,
                resolver=credential_resolver,
                event_sink=event_sink,
                policy_reviewer=policy_reviewer,
                grants=(scope or {}).get("grants"),
                container_isolated=_container_isolated,
                inputs=(scope or {}).get("inputs"),
            ) as capability_env:
                summaries.append(
                    invoke(
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
                )
        summary = "\n".join(summaries)
        if "for_each" in step_block:
            from . import v1_runtime

            v1_runtime.gather(root, state["run_id"], step_block, len(invocations))
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
        state["steps"] = _step_states(compiled, state)
        _write(root, state)
        if isinstance(exc, ExecutionError):
            raise
        raise ExecutionError(f"invalid local outcome artifacts: {exc}") from exc
    state["completed_steps"] = [*state.get("completed_steps", []), phase]
    state["status"] = (
        "awaiting_confirmation" if _ready(compiled, state["completed_steps"]) else "completed"
    )
    state["summary"] = summary[-1000:]
    state["usage_records"] = transcripts["usage_records"]
    state.pop("error", None)
    manifest = build_manifest(
        outcome_root=outcome_root,
        artifact_base=root,
        run_id=state["run_id"],
        workflow_run_id=None,
        trajectory_version=None,
        phase=phase,
        workflow_revision=compiled["workflow_revision"],
        backend_provider="outcomeci",
        state_repository=None,
        context_provider="outcomeci",
        context_revision_id=context_revision,
        repository_base_commits={repository: _local_revision(root)},
        runner=runner,
        model=chosen_model,
        transcript=transcripts,
        phase_contract=compiled["instructions"]["steps"][phase]["expects"],
    )
    (outcome_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    state["ready_steps"] = _ready(compiled, state["completed_steps"])
    state["steps"] = _step_states(compiled, state)
    _write(root, state)
    return state


def _new_run(
    compiled: dict[str, Any], intent: str, *, trigger: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The initial queued-run state trigger() builds for every trigger type."""
    state = {
        "schema_version": 3,
        "run_id": _id(intent),
        "intent": intent,
        "step": _ready(compiled, [])[0],
        "status": "queued",
        "completed_steps": [],
        "ready_steps": _ready(compiled, []),
        "created_at": datetime.now(UTC).isoformat(),
    }
    if trigger is not None:
        state["trigger"] = trigger
    return state


def _skip(state: dict[str, Any], step: str, reason: str) -> None:
    state["completed_steps"] = [*state.get("completed_steps", []), step]
    state["skipped_steps"] = [*state.get("skipped_steps", []), step]
    state.setdefault("skip_reasons", {})[step] = reason


def _settle(
    root: Path,
    compiled: dict[str, Any],
    state: dict[str, Any],
    options: ExecutionOptions,
) -> bool:
    """Resolve the v1 steps the runtime drives: skips, await steps and discussions.

    Returns True with state["step"] set when an agent step is next, or False
    once every step is done and the run is completed.
    """
    from . import v1_runtime

    while True:
        ready = _ready(compiled, state.get("completed_steps", []))
        if not ready:
            state.update({"status": "completed", "ready_steps": []})
            state["steps"] = _step_states(compiled, state)
            _write(root, state)
            return False
        step = ready[0]
        step_block = v1_runtime.block(compiled, step)
        if step_block is None:
            state["step"] = step
            return True
        reason = v1_runtime.skip_reason(root, state, step_block)
        if reason is not None:
            _skip(state, step, reason)
            continue
        if step_block["kind"] == "agent":
            state["step"] = step
            return True
        state.update({"step": step, "status": "running"})
        _write(root, state)
        try:
            if step_block["kind"] == "converse":
                v1_runtime.run_converse(root, compiled, state, step, options)
                approved = True
            else:
                approved = v1_runtime.run_await(
                    root, compiled, state, step, options.credential_resolver
                )
        except (ExecutionError, OSError) as exc:
            # Recorded as an error so `retry` can resume the step; retry sends
            # runtime-driven steps back through here, never to an agent.
            state.update({"status": "error", "error": str(exc)})
            state["steps"] = _step_states(compiled, state)
            _write(root, state)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(f"step {step} failed: {exc}") from exc
        if approved:
            state["completed_steps"] = [*state.get("completed_steps", []), step]
            continue
        _skip(state, step, "the await window expired without the signal")
        for name in compiled["instructions"]["steps"]:
            if name not in state["completed_steps"]:
                _skip(state, name, f"{step} expired")


def trigger(
    root: Path,
    config: Path,
    trigger_name: str,
    payload: dict[str, Any],
    *,
    on_created: Callable[[str], None] | None = None,
    options: ExecutionOptions = _DEFAULT_EXECUTION_OPTIONS,
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
    state = _new_run(
        compiled,
        intent,
        trigger={"name": trigger_name, "type": definition["type"], "value": payload},
    )
    if on_created is not None:
        _write(root, state)
        on_created(state["run_id"])
    if not _settle(root, compiled, state, options):
        return state
    return _execute(root, config, state, options=options)


def continue_run(
    root: Path,
    config: Path,
    run_id: str,
    approve: bool,
    *,
    options: ExecutionOptions = _DEFAULT_EXECUTION_OPTIONS,
) -> dict[str, Any]:
    state = _read(root, run_id)
    if state.get("status") != "awaiting_confirmation":
        raise ExecutionError(f"outcome cannot continue from {state.get('status')}")
    if not approve:
        raise ExecutionError("continuation requires explicit --approve")
    compiled = compile_workflow(config)
    if not _settle(root, compiled, state, options):
        return state
    state["status"] = "queued"
    _write(root, state)
    return _execute(root, config, state, options=options)


def retry(
    root: Path,
    config: Path,
    run_id: str,
    *,
    options: ExecutionOptions = _DEFAULT_EXECUTION_OPTIONS,
) -> dict[str, Any]:
    """Retry agent execution after a failure without replaying resolved gates."""
    state = _read(root, run_id)
    if state.get("status") != "error":
        raise ExecutionError(f"outcome cannot retry from {state.get('status')}")
    state["status"] = "queued"
    state.pop("error", None)
    _write(root, state)
    compiled = compile_workflow(config)
    if not _settle(root, compiled, state, options):
        return state
    return _execute(root, config, state, options=options)


def status(root: Path, run_id: str | None) -> dict[str, Any]:
    if run_id:
        return _read(root, run_id)
    records = sorted((root / ".outcomeci" / "outcomes").glob("*/run.json"), reverse=True)
    return _read(root, records[0].parent.name) if records else {"status": "no_runs"}
