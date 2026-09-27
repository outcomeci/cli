"""Readable human-hook configuration; provider identifiers stay adapter-private."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml
from outcomeci_connectors.slack import deliver as deliver_slack
from outcomeci_connectors.slack import poll_replies

from .custom import call as call_custom
from .process import ExecutionError
from .security import atomic_write_json, atomic_write_text


def assign(
    root: Path,
    config: Path,
    phase: str,
    timing: str,
    interaction_id: str,
    targets: list[tuple[str, str]],
    strategy: str,
    timeout_seconds: int | None,
    connection: str = "slack_local",
) -> dict[str, Any]:
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    try:
        phase_policy = document["spec"]["agents"]["phases"][phase]
    except (KeyError, TypeError) as exc:
        raise ExecutionError(f"human hook {phase}.{timing}.{interaction_id} was not found") from exc
    entries = [
        item
        for item in phase_policy.get("integrations", [])
        if item.get("type") == "human" and item.get("timing") == timing
    ]
    if not entries:
        entries = phase_policy.get("humans", {}).get(timing, [])
    hook = next((item for item in entries if item.get("id") == interaction_id), None)
    if hook is None:
        raise ExecutionError(f"human hook {phase}.{timing}.{interaction_id} was not found")
    connections = {
        item.get("ref"): item
        for item in document["spec"].get("connections", [])
        if isinstance(item, dict)
    }
    provider = connections.get(connection, {}).get("provider")
    if provider not in {"slack", "custom"}:
        raise ExecutionError(f"human delivery connection {connection} was not found")
    hook["delivery"] = {
        "type": provider,
        "connection": connection,
        "targets": [{"kind": kind, "name": name.strip().lstrip("@#")} for kind, name in targets],
    }
    hook["wait"] = {"strategy": strategy}
    if timeout_seconds is not None:
        hook["wait"]["timeout_seconds"] = timeout_seconds
    atomic_write_text(config, yaml.safe_dump(document, sort_keys=False))
    return {
        "phase": phase,
        "timing": timing,
        "interaction_id": interaction_id,
        "targets": hook["delivery"]["targets"],
        "wait": hook["wait"],
    }


def _custom_responses(config: Path, interaction: dict[str, Any]) -> list[dict[str, str]]:
    delivery = interaction.get("delivery", {})
    correlation_id = interaction.get("delivery_status", {}).get("correlation_id")
    if not correlation_id:
        raise ExecutionError("custom human interaction has no correlation id")
    result = call_custom(
        config, "poll", {"correlation_id": correlation_id}, str(delivery.get("connection", ""))
    )
    responses = result.get("responses", [])
    if not isinstance(responses, list):
        raise ExecutionError("custom human poll must return a responses list")
    normalized = []
    for response in responses:
        if not isinstance(response, dict) or not all(
            isinstance(response.get(key), str) and response[key]
            for key in ("from", "message", "responded_at")
        ):
            raise ExecutionError("custom human response requires from, message, and responded_at")
        normalized.append({key: response[key] for key in ("from", "message", "responded_at")})
    return normalized


def transport_responses(
    root: Path, config: Path, interaction: dict[str, Any]
) -> list[dict[str, str]]:
    delivery = interaction.get("delivery", {})
    if delivery.get("type") == "slack":
        return poll_replies(root, str(interaction["run_id"]), str(interaction["id"]))
    if delivery.get("type") == "custom":
        return _custom_responses(config, interaction)
    raise ExecutionError("human interaction has no pollable delivery")


def poll(
    root: Path,
    config: Path,
    run_id: str,
    interaction_id: str,
    wait_seconds: int = 0,
    interval_seconds: float = 2,
) -> dict[str, Any]:
    if not 0 <= wait_seconds <= 3600:
        raise ExecutionError("poll wait must be between 0 and 3600 seconds")
    deadline = time.monotonic() + wait_seconds
    outcome = root / ".outcomeci" / "outcomes" / run_id
    matches = list((outcome / "interactions").glob(f"*/{interaction_id}.json"))
    if len(matches) != 1:
        raise ExecutionError(f"interaction {interaction_id} was not found for outcome {run_id}")
    while True:
        value = json.loads(matches[0].read_text(encoding="utf-8"))
        replies = transport_responses(root, config, value)
        known = {
            (item.get("from"), item.get("message"), item.get("responded_at"))
            for item in value.get("observed_responses", [])
            if isinstance(item, dict)
        }
        for reply in replies:
            marker = (reply.get("from"), reply.get("message"), reply.get("responded_at"))
            if marker not in known:
                value.setdefault("observed_responses", []).append(reply)
                known.add(marker)
        if replies:
            atomic_write_json(matches[0], value)
        status = value.get("status", "unknown")
        result = {
            "run_id": run_id,
            "interaction_id": interaction_id,
            "status": "responded" if replies else status,
            "responses": replies,
        }
        response = value.get("response")
        if isinstance(response, dict):
            result["response"] = {
                key: response[key] for key in ("message", "responded_at") if key in response
            }
        if status != "pending" or time.monotonic() >= deadline:
            return result
        time.sleep(min(interval_seconds, max(0, deadline - time.monotonic())))


def request(
    root: Path,
    config: Path,
    run_id: str,
    interaction_id: str,
    continue_while_waiting: bool = False,
    authoritative_hook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .local import _read, _write, request_input

    state = _read(root, run_id)
    pending = state.get("pending_interaction")
    if not isinstance(pending, dict) or pending.get("id") != interaction_id:
        state = request_input(root, config, run_id, interaction_id)
        pending = state.get("pending_interaction")
    if not isinstance(pending, dict):
        raise ExecutionError(f"interaction {interaction_id} is not pending")
    path = Path(str(pending["path"]))
    interaction = json.loads(path.read_text(encoding="utf-8"))
    if authoritative_hook is not None:
        interaction["delivery"] = authoritative_hook["delivery"]
        interaction["wait"] = authoritative_hook.get("wait", {"strategy": "ask"})
    delivery = interaction.get("delivery", {})
    if delivery.get("type") == "slack":
        delivered = deliver_slack(root, interaction)
        interaction["delivery_status"] = {"delivered": True, "threads": delivered["threads"]}
    elif delivery.get("type") == "custom":
        existing_correlation = interaction.get("delivery_status", {}).get("correlation_id")
        if isinstance(existing_correlation, str) and existing_correlation:
            delivered = {
                "delivered": True,
                "targets": delivery.get("targets", []),
                "requests": 1,
                "correlation_id": existing_correlation,
                "existing": True,
            }
        else:
            payload = {
                "schema_version": "outcomeci.human-request/v1alpha1",
                "run_id": interaction["run_id"],
                "phase": interaction["phase"],
                "interaction_id": interaction["id"],
                "interaction": interaction["interaction"],
                "purpose": interaction["purpose"],
                "targets": delivery.get("targets", []),
            }
            result = call_custom(config, "request", payload, str(delivery.get("connection", "")))
            correlation_id = result.get("correlation_id")
            if not isinstance(correlation_id, str) or not correlation_id:
                raise ExecutionError("custom human request must return correlation_id")
            delivered = {
                "delivered": True,
                "targets": delivery.get("targets", []),
                "requests": 1,
                "correlation_id": correlation_id,
            }
            interaction["delivery_status"] = {"delivered": True, "correlation_id": correlation_id}
    else:
        raise ExecutionError(f"interaction {interaction_id} has no supported human delivery")
    atomic_write_json(path, interaction)
    strategy = interaction.get("wait", {}).get("strategy", "ask")
    if continue_while_waiting or strategy == "continue":
        state = _read(root, run_id)
        state.pop("pending_interaction", None)
        state.setdefault("open_interactions", []).append(
            {"id": interaction_id, "path": str(path), "phase": interaction.get("phase")}
        )
        state["status"] = "running"
        _write(root, state)
    return {
        "run_id": run_id,
        "interaction_id": interaction_id,
        **delivered,
        "wait": interaction.get("wait", {"strategy": "ask"}),
    }


def accept(
    root: Path,
    config: Path,
    run_id: str,
    interaction_id: str,
    message: str,
    approve: bool = False,
    reject: bool = False,
) -> dict[str, Any]:
    from .local import respond

    return respond(
        root, config, run_id, interaction_id, message, approve=approve, reject=reject, execute=False
    )
