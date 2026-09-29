"""Webhook trigger contract actions for webhook-trigger-v1.

Exercises the real local.trigger(): payload size limit, schema validation,
and run-state construction for a v1 `trigger: webhook` workflow. The proof
stops the run the moment its state is written, before any step executes, so
no agent is spawned and no cloud is touched: this proof is only about the
trigger's own contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ..local import trigger as local_trigger
from ..process import ExecutionError
from ..repository import validate

TRIGGER_NAME = "webhook"
FIRST_STEP = "triage"
GENERIC_INTENT = "webhook.received received"

# The webhook.received schema forbids a "subject" property (additionalProperties:
# false, unlike email.received which requires one) -- so every valid webhook
# payload falls through to this same generic intent. There is no other branch
# to exercise for this trigger type.
OVERSIZED_BODY_BASE64_LENGTH = 3 * 1024 * 1024  # comfortably over the 2 MiB trigger limit


class _Created(Exception):
    """Raised once the run's state is written, to stop before any step runs."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


def configure(root: Path) -> dict[str, Any]:
    workflow = {
        "apiVersion": "outcomeci.workflow/v1",
        "name": "webhook-trigger-proof",
        "trigger": TRIGGER_NAME,
        "steps": [
            {
                FIRST_STEP: {
                    "reason": "Summarize the webhook request in one sentence.",
                    "from": "trigger",
                    "returns": {"summary": "string"},
                }
            }
        ],
    }
    (root / "outcome.yml").write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
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


def _stop(run_id: str) -> None:
    raise _Created(run_id)


def fire(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    label = str(request["label"])
    payload = _oversized_payload() if request.get("synthetic_oversized") else request["payload"]
    expect_failure = bool(request.get("expect_failure", False))
    expect_error_contains = request.get("expect_error_contains")
    attempts = context.setdefault("webhook_attempts", {})

    try:
        local_trigger(root, root / "outcome.yml", TRIGGER_NAME, payload, on_created=_stop)
    except _Created as created:
        state = json.loads(
            (root / ".outcomeci" / "outcomes" / created.run_id / "run.json").read_text()
        )
    except ExecutionError as exc:
        attempts[label] = {"outcome": "rejected", "error": str(exc)}
        if not expect_failure:
            raise
        if expect_error_contains and expect_error_contains not in str(exc):
            raise ExecutionError(f"{label} was rejected for the wrong reason: {exc}") from exc
        return {"status": "rejected", "label": label}
    else:
        raise ExecutionError(f"{label} ran past run creation")

    attempts[label] = {
        "outcome": "accepted",
        "status": state.get("status"),
        "intent": state.get("intent"),
        "trigger": state.get("trigger"),
        "phase": state.get("phase"),
    }
    if expect_failure:
        raise ExecutionError(f"expected {label} to be rejected, but the trigger succeeded")
    return {"status": state.get("status"), "run_id": state.get("run_id"), "label": label}
