from __future__ import annotations

import json

import pytest
from test_policy_execution import executor

from outcomeci.integrations import IntegrationError


def events(broker):
    return json.loads((broker.directory / "journal.json").read_text())["events"]


@pytest.mark.parametrize("decision", ["allow", "revise", "deny"])
def test_advisor_reason_recorded_without_request_secrets(tmp_path, decision):
    def review(proposal):
        return {
            "decision": decision,
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "Matches recipient. Bearer confidential-secret U0123456789",
        }

    broker = executor(tmp_path, review)
    request = {
        "method": "POST",
        "path": "/api/chat.postMessage?private=value",
        "purpose": "Deliver notification",
        "body": {"text": "private-email-body"},
    }
    if decision == "allow":
        broker.execute("slack.request", request, step="notify")
    else:
        broker.executor.resolver = lambda _: pytest.fail("credential resolved on denial")
        with pytest.raises(IntegrationError):
            broker.execute("slack.request", request, step="notify")
    recorded = events(broker)
    assert recorded[0]["event_type"] == "integration.proposed"
    assert recorded[1]["decision"] == decision
    assert recorded[1]["reason"].startswith("Matches recipient.")
    text = json.dumps(recorded)
    assert "private-email-body" not in text and "private=value" not in text
    assert "confidential-secret" not in text and "U0123456789" not in text
    assert len({e["event_id"] for e in recorded}) == len(recorded)
    if decision == "allow":
        assert [e["event_type"] for e in recorded][-2:] == [
            "integration.started",
            "integration.completed",
        ]


def test_invalid_review_never_records_untrusted_reason(tmp_path):
    broker = executor(
        tmp_path,
        lambda _: {
            "decision": "allow",
            "proposal_sha256": "wrong",
            "reason": "private-review-content",
        },
    )
    with pytest.raises(IntegrationError):
        broker.execute(
            "slack.request", {"method": "POST", "path": "/api/chat.postMessage"}, step="notify"
        )
    assert events(broker)[-1]["decision"] == "error"
    assert "private-review-content" not in json.dumps(events(broker))
