from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_policy_execution import executor

from outcomeci.integrations import IntegrationError
from outcomeci.webhooks import ExecutionHeartbeat, ListenerUnavailable


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
        broker.execute("slack.request", request, phase="notify")
    else:
        broker.executor.resolver = lambda _: pytest.fail("credential resolved on denial")
        with pytest.raises(IntegrationError):
            broker.execute("slack.request", request, phase="notify")
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
            "slack.request", {"method": "GET", "path": "/api/users.list"}, phase="notify"
        )
    assert events(broker)[-1]["decision"] == "error"
    assert "private-review-content" not in json.dumps(events(broker))


def test_heartbeat_retries_same_ids_and_batches(tmp_path):
    directory = tmp_path / ".outcomeci/.broker/run"
    directory.mkdir(parents=True)
    entries = [{"event_id": str(n)} for n in range(101)]
    (directory / "journal.json").write_text(
        json.dumps({"events": entries, "calls": {"secret": "never-upload"}})
    )

    def request(operation, body):
        assert operation == "heartbeat"
        assert set(body) == {"lease", "events"}
        return {"policy_events_received": len(body["events"])}

    listener = SimpleNamespace(
        root=tmp_path,
        policy_events=True,
        request=Mock(
            side_effect=[
                ListenerUnavailable("offline"),
                {"policy_events_received": 100},
                {"policy_events_received": 1},
            ]
        ),
    )
    stream = ExecutionHeartbeat(listener, {"lease": "private"})
    stream.created("run")
    with pytest.raises(ListenerUnavailable):
        stream.send()
    assert stream.cursor == 0
    stream.drain()
    calls = listener.request.call_args_list
    assert calls[0].args[1]["events"] == calls[1].args[1]["events"] == entries[:100]
    assert calls[2].args[1]["events"] == entries[100:]
    assert stream.cursor == 101
    assert "never-upload" not in repr(calls)


def test_old_server_receives_ordinary_heartbeat(tmp_path):
    listener = SimpleNamespace(root=tmp_path, policy_events=False, request=Mock(return_value={}))
    stream = ExecutionHeartbeat(listener, {"lease": "private"})
    stream.created("run")
    stream.drain()
    listener.request.assert_called_once_with("heartbeat", {"lease": "private"})


@pytest.mark.parametrize("failed", [False, True])
def test_listener_drains_events_before_final_status(tmp_path, monkeypatch, failed):
    from uuid import uuid4

    from test_webhook_listener import payload, workflow

    from outcomeci import local
    from outcomeci.process import ExecutionError
    from outcomeci.webhooks import Listener

    listener = Listener("ws", "wf", tmp_path, workflow(tmp_path))
    listener.policy_events = True
    calls = []

    def request(operation, body):
        calls.append((operation, body))
        return {"policy_events_received": len(body.get("events", []))}

    monkeypatch.setattr(listener, "request", request)

    def trigger(*args, on_created):
        on_created("proof")
        d = tmp_path / ".outcomeci/.broker/proof"
        d.mkdir(parents=True)
        (d / "journal.json").write_text(json.dumps({"events": [{"event_id": "one"}]}))
        if failed:
            raise ExecutionError("policy denied")
        return {"run_id": "proof", "completed_phases": ["notify"]}

    monkeypatch.setattr(local, "trigger", trigger)
    claim = {
        "invocation_id": str(uuid4()),
        "lease_token": str(uuid4()),
        "trigger_name": "inbound",
        "trigger_type": "webhook.received",
        "input": payload(),
    }
    if failed:
        with pytest.raises(ExecutionError):
            listener.execute(claim)
    else:
        listener.execute(claim)
    assert [c[0] for c in calls] == ["start", "heartbeat", "complete"]
    assert calls[1][1]["events"] == [{"event_id": "one"}]
    assert calls[-1][1]["status"] == ("failed" if failed else "completed")
