"""The run directory's record of every brokered call and every policy decision.

A run's broker journal lives outside the run directory and dies with the
runner, while effects.json keeps only each call's outcome and the hash of its
proposal. These two files carry the rest into the run directory, so a finished
run holds the raw material a later reader needs: what each call asked for,
what came back, and why each write was allowed or refused.

The journal is credential-blind by construction (the broker injects
credentials outside it), so the records are the journal's own content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CALLS_SCHEMA = "outcomeci.calls/v1"
POLICY_SCHEMA = "outcomeci.policy/v1"
DECISION_EVENTS = {"permission.reviewed": "advisor", "permission.denied": "grants"}


def _journal(root: Path, run_id: str) -> dict[str, Any]:
    path = root / ".outcomeci" / ".broker" / run_id / "journal.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def calls_record(journal: dict[str, Any]) -> dict[str, Any]:
    """Every call the broker handled, in the order it handled them, verbatim."""
    calls = [call for call in (journal.get("calls") or {}).values() if isinstance(call, dict)]
    calls.sort(key=lambda call: (call.get("sequence") or 0, call.get("proposal_sha256") or ""))
    events = [event for event in (journal.get("events") or []) if isinstance(event, dict)]
    return {"schema_version": CALLS_SCHEMA, "calls": calls, "events": events}


def policy_record(journal: dict[str, Any]) -> dict[str, Any]:
    """Each decision about a call: the advisor's verdict with its reason, or a
    refusal by the step's grants. Joined to the call by proposal_sha256."""
    decisions: list[dict[str, Any]] = []
    for event in journal.get("events") or []:
        if not isinstance(event, dict):
            continue
        source = DECISION_EVENTS.get(str(event.get("event_type")))
        if source is None:
            continue
        decisions.append(
            {
                "sequence": len(decisions) + 1,
                "source": source,
                "decision": event.get("decision"),
                "reason": event.get("reason")
                if event.get("reason") is not None
                else event.get("message"),
                "step": event.get("step"),
                "capability": event.get("capability"),
                "proposal_sha256": event.get("proposal_sha256"),
                "occurred_at": event.get("occurred_at"),
                "event_id": event.get("event_id"),
            }
        )
    return {"schema_version": POLICY_SCHEMA, "decisions": decisions}


def write_run_records(root: Path, outcome_root: Path, run_id: str) -> list[Path]:
    """Write calls.json and policy.json into the run directory from the journal.

    Written after every step, so a run that stops early still carries the
    calls and decisions it made so far.
    """
    journal = _journal(root, run_id)
    written: list[Path] = []
    for name, record in (
        ("calls.json", calls_record(journal)),
        ("policy.json", policy_record(journal)),
    ):
        target = outcome_root / name
        target.write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        written.append(target)
    return written
