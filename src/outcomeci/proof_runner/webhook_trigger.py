"""Webhook trigger contract actions for webhook-trigger-v1.

Exercises the real, unmodified local.trigger() -- payload size limit,
schema validation, and run-state construction -- for a webhook.received
trigger, without ever reaching phase execution: the test workflow's intake
phase carries a required `before` human interaction, so a valid trigger
call returns at _open_interaction() before local.trigger() ever calls
_execute()/invoke(). No agent is spawned, no cloud is touched, and phase
execution itself is already covered by local-first-v1 -- this proof is
only about the trigger's own contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..local import trigger as local_trigger
from ..process import ExecutionError
from ..repository import validate

TRIGGER_NAME = "inbound_webhook"
BEFORE_INTERACTION_ID = "confirm_webhook_intent"
GENERIC_INTENT = "webhook.received received"

# The webhook.received schema forbids a "subject" property (additionalProperties:
# false, unlike email.received which requires one) -- so every valid webhook
# payload falls through to this same generic intent. There is no other branch
# to exercise for this trigger type.
OVERSIZED_BODY_BASE64_LENGTH = 3 * 1024 * 1024  # comfortably over the 2 MiB trigger limit


def configure(root: Path) -> dict[str, Any]:
    path = root / "outcome.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = workflow["spec"]
    spec["triggers"][TRIGGER_NAME] = {"type": "webhook.received", "delivery": "queued"}
    spec["agents"]["phases"]["intake"]["integrations"] = [
        {
            "type": "human",
            "timing": "before",
            "id": BEFORE_INTERACTION_ID,
            "participant": "requester",
            "purpose": "Confirm the webhook-originated intent before any agent work begins.",
            "interaction": "approval",
            "required": True,
        }
    ]
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
    validate(root)
    return {"status": "configured"}


def _oversized_payload() -> dict[str, Any]:
    return {
        "schema_version": "outcomeci.trigger.webhook.received/v1",
        "type": "webhook.received",
        "event_id": "evt-oversized",
        "received_at": "2026-01-01T00:00:00Z",
        "method": "POST",
        "query": "",
        "headers": {},
        "body_base64": "A" * OVERSIZED_BODY_BASE64_LENGTH,
    }


def fire(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    label = str(request["label"])
    payload = _oversized_payload() if request.get("synthetic_oversized") else request["payload"]
    expect_failure = bool(request.get("expect_failure", False))
    expect_error_contains = request.get("expect_error_contains")
    attempts = context.setdefault("webhook_attempts", {})

    try:
        state = local_trigger(root, root / "outcome.yml", TRIGGER_NAME, payload)
    except ExecutionError as exc:
        attempts[label] = {"outcome": "rejected", "error": str(exc)}
        if not expect_failure:
            raise
        if expect_error_contains and expect_error_contains not in str(exc):
            raise ExecutionError(f"{label} was rejected for the wrong reason: {exc}") from exc
        return {"status": "rejected", "label": label}

    attempts[label] = {
        "outcome": "accepted",
        "status": state.get("status"),
        "intent": state.get("intent"),
        "trigger": state.get("trigger"),
        "pending_interaction_id": (state.get("pending_interaction") or {}).get("id"),
    }
    if expect_failure:
        raise ExecutionError(f"expected {label} to be rejected, but the trigger succeeded")
    return {"status": state.get("status"), "run_id": state.get("run_id"), "label": label}
