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
    assert failed["detail"] == "integration request returned HTTP 401"


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


def test_reads_skip_the_step_policy_review(tmp_path):
    reviewed = []
    broker = executor(tmp_path, lambda proposal: reviewed.append(proposal) or allow(proposal))
    broker.execute("slack.request", {"method": "GET", "path": "/api/users.list"}, step="notify")
    assert reviewed == []
