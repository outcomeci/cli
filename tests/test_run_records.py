"""A run directory keeps every call and every policy decision from the journal."""

import json
from pathlib import Path

from outcomeci.artifacts import records as run_records

JOURNAL = {
    "calls": {
        "fp-2": {
            "as": None,
            "capability": "slack.post",
            "invocation": "inv-1",
            "proposal_sha256": "fp-2",
            "request": {"channel": "growth", "text": "hello"},
            "result": {"ok": True, "status": 200, "output": {"ts": "1.2"}},
            "review": {"decision": "allow", "reason": "Matches the header policy."},
            "sequence": 2,
            "status": "confirmed",
            "step": "share",
        },
        "fp-1": {
            "as": None,
            "capability": "x.search_recent",
            "invocation": "inv-1",
            "proposal_sha256": "fp-1",
            "request": {"query": "agents", "max_results": 10},
            "result": {"ok": True, "status": 200, "output": {"posts": []}},
            "sequence": 1,
            "status": "confirmed",
            "step": "scan",
        },
    },
    "events": [
        {
            "event_id": "e1",
            "occurred_at": "2026-10-07T15:00:01+00:00",
            "event_type": "integration.proposed",
            "step": "scan",
            "capability": "x.search_recent",
            "message": "proposed",
            "level": "info",
            "proposal_sha256": "fp-1",
        },
        {
            "event_id": "e2",
            "occurred_at": "2026-10-07T15:00:02+00:00",
            "event_type": "permission.reviewed",
            "step": "share",
            "capability": "slack.post",
            "message": "Permission advisor: allow",
            "level": "info",
            "proposal_sha256": "fp-2",
            "decision": "allow",
            "reason": "Matches the header policy.",
        },
        {
            "event_id": "e3",
            "occurred_at": "2026-10-07T15:00:03+00:00",
            "event_type": "permission.denied",
            "step": "share",
            "capability": "slack.post",
            "message": "slack.post is outside this step's grants: channel must be growth",
            "level": "warning",
            "decision": "deny",
        },
    ],
    "references": {},
}


def _journal(tmp_path: Path) -> None:
    directory = tmp_path / ".outcomeci" / ".broker" / "run-1"
    directory.mkdir(parents=True)
    (directory / "journal.json").write_text(json.dumps(JOURNAL), encoding="utf-8")


def test_calls_record_keeps_every_call_verbatim_in_broker_order() -> None:
    record = run_records.calls_record(JOURNAL)
    assert record["schema_version"] == "outcomeci.calls/v1"
    assert [call["sequence"] for call in record["calls"]] == [1, 2]
    # The body, the result and the advisor's verdict travel with the call.
    assert record["calls"][1]["request"] == {"channel": "growth", "text": "hello"}
    assert record["calls"][1]["result"]["output"] == {"ts": "1.2"}
    assert record["calls"][1]["review"]["decision"] == "allow"
    assert [event["event_type"] for event in record["events"]] == [
        "integration.proposed",
        "permission.reviewed",
        "permission.denied",
    ]


def test_policy_record_lists_advisor_verdicts_and_grant_refusals() -> None:
    record = run_records.policy_record(JOURNAL)
    assert record["schema_version"] == "outcomeci.policy/v1"
    assert record["decisions"] == [
        {
            "sequence": 1,
            "source": "advisor",
            "decision": "allow",
            "reason": "Matches the header policy.",
            "step": "share",
            "capability": "slack.post",
            "proposal_sha256": "fp-2",
            "occurred_at": "2026-10-07T15:00:02+00:00",
            "event_id": "e2",
        },
        {
            "sequence": 2,
            "source": "grants",
            "decision": "deny",
            "reason": "slack.post is outside this step's grants: channel must be growth",
            "step": "share",
            "capability": "slack.post",
            "proposal_sha256": None,
            "occurred_at": "2026-10-07T15:00:03+00:00",
            "event_id": "e3",
        },
    ]


def test_write_run_records_lands_both_files_in_the_run_directory(tmp_path: Path) -> None:
    _journal(tmp_path)
    outcome_root = tmp_path / ".outcomeci" / "outcomes" / "run-1"
    outcome_root.mkdir(parents=True)
    written = run_records.write_run_records(tmp_path, outcome_root, "run-1")
    assert [path.name for path in written] == ["calls.json", "policy.json"]
    calls = json.loads((outcome_root / "calls.json").read_text(encoding="utf-8"))
    policy = json.loads((outcome_root / "policy.json").read_text(encoding="utf-8"))
    assert len(calls["calls"]) == 2
    assert len(policy["decisions"]) == 2
    # No credential ever reaches the journal, so none reaches the record.
    text = (outcome_root / "calls.json").read_text(encoding="utf-8").lower()
    assert "authorization" not in text and "bearer" not in text


def test_write_run_records_tolerates_a_missing_journal(tmp_path: Path) -> None:
    outcome_root = tmp_path / "outcome"
    outcome_root.mkdir()
    run_records.write_run_records(tmp_path, outcome_root, "run-none")
    calls = json.loads((outcome_root / "calls.json").read_text(encoding="utf-8"))
    assert calls == {"schema_version": "outcomeci.calls/v1", "calls": [], "events": []}
