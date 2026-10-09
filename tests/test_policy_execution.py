from __future__ import annotations

import json

import httpx
import pytest
from lowered import compile_file, email_notify, email_payload

from outcomeci import policy
from outcomeci.integrations import IntegrationError, IntegrationExecutor
from outcomeci.process import ExecutionError


def executor(root, reviewer=None, handler=None):
    compiled = compile_file(email_notify(root))
    compiled["workflow"]["spec"]["connections"][0]["allow_private_network"] = True
    inner = IntegrationExecutor(
        compiled,
        resolver=lambda _: "private-token",
        transport=httpx.MockTransport(
            handler or (lambda _: httpx.Response(200, json={"ok": True}))
        ),
        reviewed=True,
    )
    return policy.PolicyExecutor(
        inner,
        root / ".broker",
        {"trigger": email_payload()},
        reviewer or allow,
        step_policy=STEP_POLICY,
    )


STEP_POLICY = {
    "content": "Step policy for notify: only message the email's requester.",
    "policy": {"runner": "codex", "model": None},
}


def allow(proposal):
    return {
        "decision": "allow",
        "proposal_sha256": proposal["proposal_sha256"],
        "reason": "Authorized",
    }


def test_reviewed_requests_are_credential_blind_and_replayed(tmp_path):
    proposals, calls = [], []

    def review(proposal):
        proposals.append(proposal)
        return allow(proposal)

    def send(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private-token"
        return httpx.Response(200, json={"ok": True, "user": {"id": "U0123456789", "name": "izzy"}})

    broker = executor(tmp_path, review, send)
    inputs = {"method": "POST", "path": "/api/chat.postMessage", "body": {"text": "Hi"}}
    first = broker.execute("slack.request", {**inputs, "purpose": "Tell @izzy"}, step="notify")
    assert first["ok"] is True
    assert "private-token" not in json.dumps(proposals)
    assert broker.execute("slack.request", {**inputs, "purpose": "Another reason"}, step="notify")[
        "replayed"
    ]
    assert len(calls) == 1
    assert len(proposals) == 1
    restored = policy.PolicyExecutor(
        broker.executor, tmp_path / ".broker", {}, review, step_policy=STEP_POLICY
    )
    assert restored.execute("slack.request", inputs, step="notify")["replayed"]


@pytest.mark.parametrize("decision", ["deny", "revise", "allow-wrong-digest"])
def test_denied_proposals_never_resolve_credentials(tmp_path, decision):
    broker = executor(tmp_path, lambda proposal: {"decision": decision, "proposal_sha256": "wrong"})
    broker.executor.resolver = lambda _: pytest.fail("credential was resolved")
    with pytest.raises(IntegrationError, match="policy did not approve"):
        broker.execute(
            "slack.request",
            {"method": "POST", "path": "/api/chat.postMessage"},
            step="notify",
        )


def test_transport_failure_is_durable_and_not_replayed(tmp_path):
    calls = []

    def send(request):
        calls.append(request)
        raise httpx.ReadTimeout("uncertain delivery")

    broker = executor(tmp_path, handler=send)
    inputs = {
        "method": "POST",
        "path": "/api/chat.postMessage",
        "body": {"text": "Hello"},
    }
    with pytest.raises(IntegrationError):
        broker.execute("slack.request", inputs, step="notify")
    with pytest.raises(IntegrationError, match="uncertain"):
        broker.execute("slack.request", inputs, step="notify")
    assert len(calls) == 1


def test_integration_failure_records_the_http_status_as_detail(tmp_path):
    def send(request):
        return httpx.Response(401, json={"error": "invalid_auth"})

    broker = executor(tmp_path, handler=send)
    inputs = {
        "method": "POST",
        "path": "/api/chat.postMessage",
        "body": {"text": "Hello"},
    }
    with pytest.raises(IntegrationError):
        broker.execute("slack.request", inputs, step="notify")

    journal = json.loads((tmp_path / ".broker" / "journal.json").read_text())
    failed = next(e for e in journal["events"] if e["event_type"] == "integration.failed")
    assert failed["detail"] == "integration request returned HTTP 401: invalid_auth"
    assert failed["http_status"] == 401
    assert failed["message"] == "Integration request refused by the provider"
    (call,) = journal["calls"].values()
    assert call["status"] == "failed"
    assert call["result"] == {
        "ok": False,
        "status": 401,
        "error": "integration request returned HTTP 401: invalid_auth",
    }


def test_integration_failure_detail_is_scrubbed_of_credential_shaped_text(tmp_path):
    def raise_with_token_shaped_text(_reference):
        raise ExecutionError("connect failed: Bearer some-token-value rejected")

    broker = executor(tmp_path, handler=lambda _: httpx.Response(200, json={"ok": True}))
    broker.executor.resolver = raise_with_token_shaped_text
    inputs = {
        "method": "POST",
        "path": "/api/chat.postMessage",
        "body": {"text": "Hello"},
    }
    with pytest.raises(ExecutionError):
        broker.execute("slack.request", inputs, step="notify")

    journal = json.loads((tmp_path / ".broker" / "journal.json").read_text())
    failed = next(e for e in journal["events"] if e["event_type"] == "integration.failed")
    assert "some-token-value" not in failed["detail"]
    assert "[credential withheld]" in failed["detail"]
    assert "connect failed" in failed["detail"]


def test_provider_errors_fail_safely(tmp_path):
    broker = executor(tmp_path)
    broker.executor.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
    )
    with pytest.raises(IntegrationError, match=r"provider rejected.*invalid_auth"):
        broker.execute("slack.request", {"method": "GET", "path": "/api/failure"}, step="notify")

    broker.executor.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"ok": False, "error": "unsafe detail containing spaces: private"}
        )
    )
    with pytest.raises(IntegrationError, match=r"provider rejected the request$"):
        broker.execute(
            "slack.request", {"method": "GET", "path": "/api/unsafe-failure"}, step="notify"
        )


def test_provider_echo_cannot_disclose_injected_credential(tmp_path):
    broker = executor(
        tmp_path,
        handler=lambda request: httpx.Response(
            200, json={"headers": {"Authorization": request.headers["Authorization"]}}
        ),
    )
    result = broker.execute("slack.request", {"method": "GET", "path": "/api/echo"}, step="notify")
    assert "private-token" not in json.dumps(result)
    assert "credential withheld" in json.dumps(result)


def test_budget_methods_and_origin_cannot_be_expanded(tmp_path):
    broker = executor(tmp_path)
    broker.executor.compiled["workflow"]["spec"]["integrations"]["slack"]["access"][
        "max_requests"
    ] = 1
    for inputs in [
        {"method": "DELETE", "path": "/api/users"},
        {"method": "GET", "path": "https://evil.example/api"},
    ]:
        with pytest.raises(IntegrationError):
            broker.execute("slack.request", inputs, step="notify")
    with pytest.raises(IntegrationError, match="budget"):
        broker.execute("slack.request", {"method": "GET", "path": "/api/new"}, step="notify")


def test_request_budget_is_per_agent_run_not_per_workflow_run(tmp_path):
    # Steps and for_each items each get their own broker over one run journal.
    first, second = executor(tmp_path), executor(tmp_path)
    for broker in (first, second):
        broker.executor.compiled["workflow"]["spec"]["integrations"]["slack"]["access"][
            "max_requests"
        ] = 1
    first.execute("slack.request", {"method": "GET", "path": "/api/users.list"}, step="notify")
    with pytest.raises(IntegrationError, match="budget"):
        first.execute("slack.request", {"method": "GET", "path": "/api/channels"}, step="notify")
    result = second.execute(
        "slack.request", {"method": "GET", "path": "/api/conversations"}, step="notify"
    )
    assert result["ok"] is True


def test_reads_skip_the_step_policy_review(tmp_path):
    reviewed = []
    broker = executor(tmp_path, lambda proposal: reviewed.append(proposal) or allow(proposal))
    broker.execute("slack.request", {"method": "GET", "path": "/api/users.list"}, step="notify")
    assert reviewed == []


def test_receipts_keep_recorded_order_across_journal_roundtrips(tmp_path):
    proposals = []

    def review(proposal):
        proposals.append(proposal)
        if len(proposals) == 1:
            return {**allow(proposal), "decision": "deny"}
        return allow(proposal)

    broker = executor(tmp_path, review)
    for index in range(8):
        request = {"method": "POST", "path": "/api/chat.postMessage", "body": {"text": str(index)}}
        if index == 0:
            with pytest.raises(IntegrationError, match="policy did not approve"):
                broker.execute("slack.request", request, step="notify")
        else:
            broker.execute("slack.request", request, step="notify")
    receipts = proposals[-1]["receipts"]
    assert [item["sequence"] for item in receipts] == list(range(1, 9))
    assert [item["status"] for item in receipts] == ["denied"] + ["confirmed"] * 6 + ["reviewing"]
    assert "result" not in receipts[0] and "result" not in receipts[-1]
    assert receipts[-1]["proposal_sha256"] == proposals[-1]["proposal_sha256"]
    assert all(item["result"]["ok"] for item in receipts[1:-1])
    # Dynamic HTTP's root body exposure never enters the review.
    assert all("output" not in item["result"] for item in receipts[1:-1])


def test_receipt_result_contains_only_bounded_explicit_effect_identifiers():
    output = {
        "ts": "123.456",
        "channel": "C123",
        "id": "x" * 501,
        "number": 7,
        "sha": {"secret": "private"},
        "ref": "private-token",
        "result": {"access_token": "private-token"},
        "token": "private-token",
    }
    call = {"status": "confirmed", "result": {"ok": True, "status": 200, "output": output}}
    operation = {
        "response": {
            "expose": {
                "ts": "body.ts",
                "channel": "body.channel",
                "id": "body.id",
                "number": "body.number",
                "sha": "body.sha",
                "ref": "body.access_token",
                "result": "body",
                "token": "body.token",
            }
        }
    }
    projected = policy._receipt_result(call, operation)
    assert projected == {
        "result": {
            "ok": True,
            "status": 200,
            "output": {"ts": "123.456", "channel": "C123", "number": 7},
        }
    }
    assert "private" not in json.dumps(projected)
    for status in ("denied", "unsent", "pending", "uncertain", "reviewing"):
        assert policy._receipt_result({**call, "status": status}, operation) == {}
    output["result"]["ok"] = False
    assert policy._receipt_result(call, operation) == {"result": {"ok": False, "status": 200}}


def test_receipt_identifiers_use_credential_redacted_connector_output(tmp_path):
    proposals = []

    def review(proposal):
        proposals.append(proposal)
        return allow(proposal)

    broker = executor(
        tmp_path,
        review,
        lambda _: httpx.Response(
            200,
            json={
                "ok": True,
                "ts": "123.456",
                "channel": "private-token",
                "token": "private-token",
            },
        ),
    )
    operation = broker.executor.compiled["workflow"]["spec"]["integrations"]["slack"]["operations"][
        "request"
    ]
    operation["response"]["expose"].update(
        {"ts": "body.ts", "channel": "body.channel", "token": "body.token"}
    )
    for text in ("header", "reply"):
        broker.execute(
            "slack.request",
            {"method": "POST", "path": "/api/chat.postMessage", "body": {"text": text}},
            step="notify",
        )
    result = proposals[1]["receipts"][0]["result"]
    assert result["output"]["ts"] == "123.456"
    assert "private-token" not in json.dumps(result)
    assert "token" not in result["output"]


def test_a_refused_write_is_recorded_as_failed_and_still_never_resent(tmp_path):
    """X answers a reply it will not allow with 403 and a reason. The receipt
    keeps the status and the reason, and the write is not sent a second time."""
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            403,
            json={
                "title": "Forbidden",
                "detail": "Reply to this conversation is not allowed because you have "
                "not been mentioned or otherwise engaged by the author.",
                "type": "about:blank",
                "status": 403,
            },
        )

    broker = executor(tmp_path, handler=send)
    inputs = {"method": "POST", "path": "/api/chat.postMessage", "body": {"text": "Hi"}}
    with pytest.raises(IntegrationError, match="not been mentioned"):
        broker.execute("slack.request", inputs, step="notify")
    with pytest.raises(IntegrationError, match="refused"):
        broker.execute("slack.request", inputs, step="notify")
    assert len(calls) == 1
    journal = json.loads((tmp_path / ".broker" / "journal.json").read_text())
    (call,) = journal["calls"].values()
    assert call["status"] == "failed"
    assert call["result"]["status"] == 403
    assert call["result"]["error"].startswith(
        "integration request returned HTTP 403: Forbidden | Reply to this conversation"
    )


@pytest.mark.parametrize("status", [500, 503, 408])
def test_a_server_error_or_timeout_status_stays_uncertain(tmp_path, status):
    """A 5xx or 408 may still have been processed, so its delivery is unknown."""

    def send(request):
        return httpx.Response(status, json={"message": "try later"})

    broker = executor(tmp_path, handler=send)
    inputs = {"method": "POST", "path": "/api/chat.postMessage", "body": {"text": "Hi"}}
    with pytest.raises(IntegrationError, match="try later"):
        broker.execute("slack.request", inputs, step="notify")
    journal = json.loads((tmp_path / ".broker" / "journal.json").read_text())
    (call,) = journal["calls"].values()
    assert call["status"] == "uncertain"
    assert "result" not in call or call["result"] is None


def test_the_provider_reason_is_bounded_and_never_carries_the_credential(tmp_path):
    def send(request):
        return httpx.Response(
            400,
            json={
                "message": "bad token private-token " + "x" * 2000,
                "documentation_url": "https://example.com",
            },
        )

    broker = executor(tmp_path, handler=send)
    inputs = {"method": "POST", "path": "/api/chat.postMessage", "body": {"text": "Hi"}}
    with pytest.raises(IntegrationError) as raised:
        broker.execute("slack.request", inputs, step="notify")
    reason = str(raised.value).split(": ", 1)[1]
    assert "private-token" not in str(raised.value)
    assert len(reason) <= 300
    assert "documentation_url" not in str(raised.value)


@pytest.mark.parametrize("schema_read", [False, True])
def test_repeated_reads_fetch_fresh_results_and_consume_budget(tmp_path, schema_read):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "revision": len(calls)})

    broker = executor(tmp_path, handler=send)
    integration = broker.executor.compiled["workflow"]["spec"]["integrations"]["slack"]
    integration["access"]["max_requests"] = 2
    inputs = {"method": "GET", "path": "/api/users.list"}
    if schema_read:
        operation = integration["operations"]["request"]
        operation["request"] = {
            "method": "POST",
            "path": "/api/report",
            "headers": {},
            "timeout_seconds": 30,
        }
        operation["input"] = {"type": "object"}
        operation["policy"] = {"side_effect": "read"}
        inputs = {}

    first = broker.execute("slack.request", inputs, step="notify")
    second = broker.execute("slack.request", inputs, step="notify")
    assert first["output"]["result"]["revision"] == 1
    assert second["output"]["result"]["revision"] == 2
    assert first["receipt"] != second["receipt"]
    with pytest.raises(IntegrationError, match="budget"):
        broker.execute("slack.request", inputs, step="notify")
    assert len(calls) == 2

    restored = policy.PolicyExecutor(
        broker.executor, tmp_path / ".broker", {}, allow, step_policy=STEP_POLICY
    )
    assert (
        restored.execute("slack.request", inputs, step="notify")["output"]["result"]["revision"]
        == 3
    )
