"""Readable human-hook configuration; provider identifiers stay adapter-private."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from .process import ExecutionError
from .slack import deliver as deliver_slack
from .slack import poll_replies


def assign(root: Path, config: Path, phase: str, timing: str, interaction_id: str, targets: list[tuple[str, str]], strategy: str, timeout_seconds: int | None) -> dict[str, Any]:
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    try:
        entries = document["spec"]["agents"]["phases"][phase]["humans"][timing]
    except (KeyError, TypeError) as exc:
        raise ExecutionError(f"human hook {phase}.{timing}.{interaction_id} was not found") from exc
    hook = next((item for item in entries if item.get("id") == interaction_id), None)
    if hook is None:
        raise ExecutionError(f"human hook {phase}.{timing}.{interaction_id} was not found")
    hook["delivery"] = {"type": "slack", "connection": "slack_local", "targets": [
        {"kind": kind, "name": name.strip().lstrip("@#")} for kind, name in targets
    ]}
    hook["wait"] = {"strategy": strategy}
    if timeout_seconds is not None:
        hook["wait"]["timeout_seconds"] = timeout_seconds
    temporary = config.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    temporary.replace(config)
    return {"phase": phase, "timing": timing, "interaction_id": interaction_id, "targets": hook["delivery"]["targets"], "wait": hook["wait"]}


def poll(root: Path, run_id: str, interaction_id: str, wait_seconds: int = 0, interval_seconds: float = 2) -> dict[str, Any]:
    if not 0 <= wait_seconds <= 3600:
        raise ExecutionError("poll wait must be between 0 and 3600 seconds")
    deadline = time.monotonic() + wait_seconds
    outcome = root / ".outcomeci" / "outcomes" / run_id
    matches = list((outcome / "interactions").glob(f"*/{interaction_id}.json"))
    if len(matches) != 1:
        raise ExecutionError(f"interaction {interaction_id} was not found for outcome {run_id}")
    while True:
        value = json.loads(matches[0].read_text(encoding="utf-8"))
        replies = poll_replies(root, run_id, interaction_id)
        known = {(item.get("from"), item.get("message"), item.get("responded_at")) for item in value.get("observed_responses", []) if isinstance(item, dict)}
        for reply in replies:
            marker = (reply.get("from"), reply.get("message"), reply.get("responded_at"))
            if marker not in known:
                value.setdefault("observed_responses", []).append(reply)
                known.add(marker)
        if replies:
            temporary = matches[0].with_suffix(".tmp")
            temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(matches[0])
        status = value.get("status", "unknown")
        result = {"run_id": run_id, "interaction_id": interaction_id, "status": "responded" if replies else status, "responses": replies}
        response = value.get("response")
        if isinstance(response, dict):
            result["response"] = {key: response[key] for key in ("message", "responded_at") if key in response}
        if status != "pending" or time.monotonic() >= deadline:
            return result
        time.sleep(min(interval_seconds, max(0, deadline - time.monotonic())))


def request(root: Path, config: Path, run_id: str, interaction_id: str, continue_while_waiting: bool = False, authoritative_hook: dict[str, Any] | None = None) -> dict[str, Any]:
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
    if interaction.get("delivery", {}).get("type") != "slack":
        raise ExecutionError(f"interaction {interaction_id} is not configured for Slack")
    delivered = deliver_slack(root, interaction)
    interaction["delivery_status"] = {"delivered": True, "threads": delivered["threads"]}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(interaction, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    strategy = interaction.get("wait", {}).get("strategy", "ask")
    if continue_while_waiting or strategy == "continue":
        state = _read(root, run_id)
        state.pop("pending_interaction", None)
        state.setdefault("open_interactions", []).append({"id": interaction_id, "path": str(path), "phase": interaction.get("phase")})
        state["status"] = "running"
        _write(root, state)
    return {"run_id": run_id, "interaction_id": interaction_id, **delivered, "wait": interaction.get("wait", {"strategy": "ask"})}


def accept(root: Path, config: Path, run_id: str, interaction_id: str, message: str, approve: bool = False, reject: bool = False) -> dict[str, Any]:
    from .local import respond

    return respond(root, config, run_id, interaction_id, message, approve=approve, reject=reject, execute=False)
