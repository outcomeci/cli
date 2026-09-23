from __future__ import annotations

import json

import httpx
import pytest
import yaml
from test_typed_contracts import email_payload, typed_workflow

from outcomeci import capability, local, policy
from outcomeci.config import compile_workflow
from outcomeci.integrations import IntegrationError, IntegrationExecutor
from outcomeci.process import ExecutionError


def executor(root, reviewer=None, handler=None):
    compiled = compile_workflow(typed_workflow(root))
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
        inner, root / ".broker", {"trigger": email_payload()}, reviewer or allow
    )


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
    inputs = {"method": "GET", "path": "/api/users.list", "purpose": "Find @izzy"}
    first = broker.execute("slack.request", inputs, phase="notify")
    assert "U0123456789" not in json.dumps(first)
    assert "private-token" not in json.dumps(proposals)
    assert first["output"]["result"]["user"]["id"] == "ref:izzy:id"
    assert broker.execute("slack.request", {**inputs, "purpose": "Another reason"}, phase="notify")[
        "replayed"
    ]
    assert len(calls) == 1
    restored = policy.PolicyExecutor(broker.executor, tmp_path / ".broker", {}, review)
    assert restored.execute("slack.request", inputs, phase="notify")["replayed"]


@pytest.mark.parametrize("decision", ["deny", "revise", "allow-wrong-digest"])
def test_denied_proposals_never_resolve_credentials(tmp_path, decision):
    broker = executor(tmp_path, lambda proposal: {"decision": decision, "proposal_sha256": "wrong"})
    broker.executor.resolver = lambda _: pytest.fail("credential was resolved")
    with pytest.raises(IntegrationError, match="policy did not approve"):
        broker.execute(
            "slack.request",
            {"method": "POST", "path": "/api/chat.postMessage"},
            phase="notify",
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
        broker.execute("slack.request", inputs, phase="notify")
    with pytest.raises(IntegrationError, match="uncertain"):
        broker.execute("slack.request", inputs, phase="notify")
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
        broker.execute("slack.request", inputs, phase="notify")

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
        broker.execute("slack.request", inputs, phase="notify")

    journal = json.loads((tmp_path / ".broker" / "journal.json").read_text())
    failed = next(e for e in journal["events"] if e["event_type"] == "integration.failed")
    assert "some-token-value" not in failed["detail"]
    assert "[credential withheld]" in failed["detail"]
    assert "connect failed" in failed["detail"]


def test_ambiguous_names_and_provider_errors_fail_safely(tmp_path):
    broker = executor(
        tmp_path,
        handler=lambda _: httpx.Response(
            200,
            json={
                "ok": True,
                "members": [
                    {"name": "izzy", "id": "U0123456789"},
                    {"name": "izzy", "id": "U0987654321"},
                ],
            },
        ),
    )
    result = broker.execute(
        "slack.request", {"method": "GET", "path": "/api/users.list"}, phase="notify"
    )
    assert "U0123456789" not in json.dumps(result)
    with pytest.raises(IntegrationError, match="ambiguous"):
        broker.execute(
            "slack.request",
            {
                "method": "POST",
                "path": "/api/conversations.open",
                "body": {"users": "ref:izzy:id"},
            },
            phase="notify",
        )
    broker.executor.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
    )
    with pytest.raises(IntegrationError, match=r"provider rejected.*invalid_auth"):
        broker.execute("slack.request", {"method": "GET", "path": "/api/failure"}, phase="notify")

    broker.executor.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"ok": False, "error": "unsafe detail containing spaces: private"}
        )
    )
    with pytest.raises(IntegrationError, match=r"provider rejected the request$"):
        broker.execute(
            "slack.request", {"method": "GET", "path": "/api/unsafe-failure"}, phase="notify"
        )


def test_provider_echo_cannot_disclose_injected_credential(tmp_path):
    broker = executor(
        tmp_path,
        handler=lambda request: httpx.Response(
            200, json={"headers": {"Authorization": request.headers["Authorization"]}}
        ),
    )
    result = broker.execute("slack.request", {"method": "GET", "path": "/api/echo"}, phase="notify")
    assert "private-token" not in json.dumps(result)
    assert "credential withheld" in json.dumps(result)


def test_budget_methods_origin_and_identifiers_cannot_be_expanded(tmp_path):
    broker = executor(tmp_path)
    broker.executor.compiled["workflow"]["spec"]["integrations"]["slack"]["access"][
        "max_requests"
    ] = 1
    for inputs in [
        {"method": "DELETE", "path": "/api/users"},
        {"method": "GET", "path": "https://evil.example/api"},
        {"method": "POST", "path": "/api/test", "body": {"user": "U0123456789"}},
    ]:
        with pytest.raises(IntegrationError):
            broker.execute("slack.request", inputs, phase="notify")
    with pytest.raises(IntegrationError, match="budget"):
        broker.execute("slack.request", {"method": "GET", "path": "/api/new"}, phase="notify")


@pytest.mark.parametrize("execution_backend", ["filesystem", "outcomeci"])
def test_normal_email_agent_broker_policy_http_flow(tmp_path, monkeypatch, execution_backend):
    path = typed_workflow(tmp_path)
    if execution_backend == "outcomeci":
        value = yaml.safe_load(path.read_text())
        value["spec"]["backend"]["provider"] = "outcomeci"
        path.write_text(yaml.safe_dump(value, sort_keys=False))
    requests, reviews, events = [], [], []
    responses = [
        {"ok": True, "members": [{"id": "U0123456789", "name": "izzy"}]},
        {"ok": True, "channel": {"id": "D0123456789"}},
        {"ok": True, "channel": "D0123456789", "ts": "1234.567"},
    ]

    def send(request):
        requests.append(request)
        if request.url.path == "/api/conversations.open":
            assert json.loads(request.content)["users"] == "U0123456789"
        if request.url.path == "/api/chat.postMessage":
            assert json.loads(request.content)["channel"] == "D0123456789"
        return httpx.Response(200, json=responses.pop(0))

    original = capability.IntegrationExecutor

    def inner(compiled, **kwargs):
        compiled["workflow"]["spec"]["connections"][0]["allow_private_network"] = True
        return original(
            compiled,
            resolver=lambda _: "private-token",
            transport=httpx.MockTransport(send),
            reviewed=True,
        )

    monkeypatch.setattr(capability, "IntegrationExecutor", inner)
    monkeypatch.setattr(
        policy.PolicyExecutor,
        "_review",
        lambda self, proposal: reviews.append(proposal) or allow(proposal),
    )

    def agent(runner, model, prompt, root, timeout, **kwargs):
        assert email_payload()["subject"] in prompt
        assert kwargs["container_isolated"] is (execution_backend == "outcomeci")
        # Drive the actual run-scoped Unix broker, exactly as the agent's CLI tool does.
        for key, value in kwargs["extra_env"].items():
            monkeypatch.setenv(key, value)
        users = capability.invoke_integration(
            "slack.request",
            {"method": "GET", "path": "/api/users.list", "purpose": "Find @izzy"},
        )
        user = users["output"]["result"]["members"][0]["id"]
        assert user == "ref:izzy:id"
        dm = capability.invoke_integration(
            "slack.request",
            {
                "method": "POST",
                "path": "/api/conversations.open",
                "body": {"users": user},
                "purpose": "Open recipient DM",
            },
        )
        channel = dm["output"]["result"]["channel"]["id"]
        sent = capability.invoke_integration(
            "slack.request",
            {
                "method": "POST",
                "path": "/api/chat.postMessage",
                "body": {"channel": channel, "text": email_payload()["text_body"]},
                "purpose": "Deliver email content",
            },
        )
        assert "D0123456789" not in json.dumps(sent)
        output = next(item for item in kwargs["writable_paths"] if item.name == "delivery.json")
        output.write_text(json.dumps({"status": "delivered"}))
        return "Email delivered"

    monkeypatch.setattr(local, "invoke", agent)
    state = local.trigger(
        tmp_path,
        path,
        "inbound",
        email_payload(),
        options=local.ExecutionOptions(
            credential_resolver=(lambda _: "private-token"),
            event_sink=events.append,
            execution_backend=execution_backend,
            _container_isolated=execution_backend == "outcomeci",
        ),
    )
    assert state["completed_phases"] == ["notify"]
    assert len(requests) == len(reviews) == 3
    assert [event["event_type"] for event in events] == [
        event_type
        for _ in range(3)
        for event_type in (
            "integration.proposed",
            "permission.reviewed",
            "integration.started",
            "integration.completed",
        )
    ]
    assert all(review["context"]["trigger"]["value"] == email_payload() for review in reviews)
    assert "private-token" not in json.dumps(reviews)


def test_cloud_backend_requires_scoped_credential_resolver(tmp_path):
    path = typed_workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["backend"]["provider"] = "outcomeci"
    path.write_text(yaml.safe_dump(value, sort_keys=False))

    with pytest.raises(ExecutionError, match="scoped credential resolver"):
        local.trigger(
            tmp_path,
            path,
            "inbound",
            email_payload(),
            options=local.ExecutionOptions(execution_backend="outcomeci"),
        )


def test_filesystem_backend_cannot_claim_container_isolation(tmp_path):
    path = typed_workflow(tmp_path)

    with pytest.raises(ExecutionError, match="reserved for OutcomeCI"):
        local.trigger(
            tmp_path,
            path,
            "inbound",
            email_payload(),
            options=local.ExecutionOptions(_container_isolated=True),
        )
