"""Managed media processor calls: compile rules, dispatch, polling, cancel, journal."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from outcomeci_connectors.provider import Grantable, NoAuth, Operation, Provider
from outcomeci_connectors.providers.media import PROVIDER as MEDIA
from outcomeci_connectors.providers.media import _MediaProvider
from outcomeci_connectors.providers.tavily import PROVIDER as TAVILY

from outcomeci.artifacts import dataset
from outcomeci.artifacts import records as run_records
from outcomeci.broker import pending
from outcomeci.broker.executor import IntegrationError, IntegrationExecutor
from outcomeci.broker.policy import PolicyExecutor
from outcomeci.cloud_runner.client import CoreClient, CoreError
from outcomeci.cloud_runner.main import workflow_failure_category
from outcomeci.workflow import language
from outcomeci.workflow.compiler import ConfigError, compile_workflow
from outcomeci.workflow.language import duration_seconds

SRC = Path(__file__).resolve().parents[1] / "src" / "outcomeci"
INPUT = {"attachment": "att_01", "outputs": ["transcript"]}
COMPLETED = {
    "state": "completed",
    "ok": True,
    "status": 200,
    "output": {
        "media_type": "audio",
        "outputs": {"transcript": {"text": "hello", "language": "en"}},
        "provider": "deepgram",
        "model": "nova-3",
        "duration_seconds": 61.0,
        "usage": {"minutes": 1.02},
    },
    "audit": {"minutes": 1.02},
}
PENDING = {"state": "pending", "retry_after_seconds": 10, "audit": {}}


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(language, "providers", lambda: {MEDIA.name: MEDIA, TAVILY.name: TAVILY})


@dataclass(frozen=True)
class _StaleMedia(Provider):
    """A `media` contract from before the registry shipped."""

    def contract(self):
        return {**super().contract(), "media": {}}


def _media_like(name="media", **overrides):
    return _MediaProvider(
        name=name,
        base_url=None,
        auth=(NoAuth(),),
        max_requests=10,
        managed=True,
        operations=MEDIA.operations,
        registry=MEDIA.registry,
        **overrides,
    )


def document(*, trigger="email", can=None, binding=None, step=None, apis=None):
    return {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": trigger,
        "secrets": {"key": "vault:tavily/key"},
        "apis": apis or {"transcribe": binding or {"uses": "media"}},
        "steps": [
            {
                "notes": {
                    "reason": "Transcribe the recording.",
                    "can": can
                    if can is not None
                    else [{"transcribe.process": {"outputs": ["transcript"]}}],
                    **(step or {}),
                }
            }
        ],
    }


def compiled(tmp_path, doc=None, **kwargs):
    path = tmp_path / "notes.outcome.yaml"
    path.write_text(yaml.safe_dump(doc or document(**kwargs)))
    return compile_workflow(path)


def connection(value):
    return value["workflow"]["spec"]["connections"][0]


def broker(tmp_path, value, *, step="notes", **kwargs):
    grants = value["instructions"]["steps"][step]["v1"]["grants"]
    grants = [{**g, "args": {k: v["literal"] for k, v in g["args"].items()}} for g in grants]
    executor_kwargs = {
        key: kwargs.pop(key)
        for key in ("connector_client", "resolver", "transport")
        if key in kwargs
    }
    return PolicyExecutor(
        IntegrationExecutor(value, reviewed=True, **executor_kwargs),
        tmp_path / "broker",
        {},
        grants=grants,
        sleep=lambda _: None,
        **kwargs,
    )


def journal(tmp_path):
    return json.loads((tmp_path / "broker" / "journal.json").read_text())


def events(tmp_path, kind=None):
    return [
        item for item in journal(tmp_path)["events"] if kind is None or item["event_type"] == kind
    ]


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# Compile rules (D1, D6)


def test_media_binding_compiles_to_a_managed_connection_without_credential(tmp_path):
    value = compiled(tmp_path)
    assert connection(value) == {
        "ref": "transcribe",
        "provider": "http",
        "base_url": None,
        "managed": True,
        "auth": {
            "connector": "media",
            "accepts": MEDIA.contract()["auth"]["accepts"],
            "managed": True,
        },
        "allow_private_network": False,
    }
    assert value["connectors"]["transcribe"]["provider"] == "media"
    assert value["validation_warnings"] == []
    assert "registry" in value["workflow"]["spec"]["integrations"]["transcribe"]["media"]


def test_media_rejects_auth(tmp_path):
    with pytest.raises(ConfigError, match="apis.transcribe.auth is not used: media takes no"):
        compiled(tmp_path, binding={"uses": "media", "auth": "secrets.key"})


def test_managed_grants_are_static(tmp_path):
    with pytest.raises(ConfigError, match="grants on a managed api must be static values"):
        compiled(tmp_path, can=[{"transcribe.process": {"outputs": "trigger.wanted"}}])


def test_unknown_output_type_names_the_type(tmp_path):
    with pytest.raises(ConfigError, match=r"'subtitles'.*transcript"):
        compiled(tmp_path, can=[{"transcribe.process": {"outputs": ["subtitles"]}}])


@pytest.mark.parametrize("outputs", ["transcript", []])
def test_outputs_must_be_a_list_of_strings(tmp_path, outputs):
    with pytest.raises(ConfigError, match=r"\.outputs must be a non-empty list"):
        compiled(tmp_path, can=[{"transcribe.process": {"outputs": outputs}}])


def test_stale_contract_is_a_compile_error(tmp_path, monkeypatch):
    stale = _StaleMedia(
        name="media",
        base_url=None,
        auth=(NoAuth(),),
        managed=True,
        operations=MEDIA.operations,
    )
    monkeypatch.setattr(language, "providers", lambda: {"media": stale})
    with pytest.raises(ConfigError, match="outcomeci-connectors>=0.16.0"):
        compiled(tmp_path)


def test_contract_without_managed_key_is_an_ordinary_provider(tmp_path, monkeypatch):
    plain = Provider(
        name="plain",
        base_url="https://plain.test",
        auth=(NoAuth(),),
        operations={
            "ping": Operation(
                description="Ping.",
                method="GET",
                path="/ping",
                input={"type": "object"},
                expose={"result": "body"},
                grantable={"outputs": Grantable(field="outputs")},
            )
        },
    )
    assert "managed" not in plain.contract()
    monkeypatch.setattr(language, "providers", lambda: {"plain": plain})
    value = compiled(
        tmp_path,
        apis={"svc": {"uses": "plain"}},
        can=[{"svc.ping": {"outputs": "trigger.wanted"}}],
    )
    assert connection(value)["managed"] is False
    assert "managed" not in connection(value)["auth"]


@pytest.mark.parametrize(
    "trigger",
    ["manual", {"type": "cron", "expression": "*/15 * * * *", "timezone": "UTC"}],
)
def test_other_triggers_warn_about_attachment_source(tmp_path, trigger):
    value = compiled(tmp_path, trigger=trigger)
    (warning,) = value["validation_warnings"]
    assert "apis.transcribe" in warning and "media.process" in warning
    assert compiled(tmp_path, trigger="email")["validation_warnings"] == []


def test_media_step_defaults_to_a_30_minute_timeout(tmp_path):
    value = compiled(tmp_path)
    assert value["instructions"]["steps"]["notes"]["v1"]["timeout_seconds"] == 1800
    search = compiled(
        tmp_path,
        apis={"web": {"uses": "tavily"}},
        can=[{"web.search": {"topic": "general"}}],
    )
    assert "timeout_seconds" not in search["instructions"]["steps"]["notes"]["v1"]


def test_explicit_timeout_is_lowered(tmp_path):
    value = compiled(tmp_path, step={"timeout": "5m"})
    assert value["instructions"]["steps"]["notes"]["v1"]["timeout_seconds"] == duration_seconds(
        "5m", "x"
    )
    with pytest.raises(ConfigError, match="steps.notes.timeout must be a duration"):
        compiled(tmp_path, step={"timeout": "soon"})


def test_no_tavily_literal_in_sources():
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if re.search(r"tavily", path.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    assert offenders == []


# Dispatch and local runs (D2)


def test_managed_call_routes_through_connector_client_with_fingerprint(tmp_path):
    value = compiled(tmp_path)
    calls = []

    def proxy(**call):
        assert call["request_id"] in journal(tmp_path)["calls"]
        calls.append(call)
        return dict(COMPLETED)

    result = broker(
        tmp_path,
        value,
        connector_client=proxy,
        resolver=lambda _: pytest.fail("credential resolved"),
        transport=httpx.MockTransport(lambda request: pytest.fail("network used")),
    ).execute("transcribe.process", INPUT, step="notes")
    assert result["output"]["outputs"]["transcript"]["text"] == "hello"
    assert calls == [
        {
            "step": "notes",
            "capability": "transcribe.process",
            "request_id": result["receipt"],
            "input": INPUT,
        }
    ]
    assert len(result["receipt"]) == 64


def test_local_run_fails_before_any_request(tmp_path):
    value = compiled(tmp_path)
    runtime = broker(
        tmp_path,
        value,
        transport=httpx.MockTransport(lambda request: pytest.fail("network used")),
    )
    with pytest.raises(IntegrationError, match="media runs only in managed runs") as raised:
        runtime.execute("transcribe.process", INPUT, step="notes")
    assert raised.value.code == "integration.managed_unavailable"
    (call,) = journal(tmp_path)["calls"].values()
    assert call["status"] == "failed" or call["status"] == "uncertain"
    assert "attempts" not in call


def test_receipt_required_without_request_id(tmp_path):
    executor = IntegrationExecutor(
        compiled(tmp_path), reviewed=True, connector_client=lambda **_: {}
    )
    with pytest.raises(IntegrationError, match="durable receipt") as raised:
        executor.execute("transcribe.process", INPUT, step="notes", request_id=None)
    assert raised.value.code == "integration.receipt_required"


def test_response_without_state_is_completed(tmp_path):
    executor = IntegrationExecutor(
        compiled(tmp_path),
        reviewed=True,
        connector_client=lambda **_: {k: v for k, v in COMPLETED.items() if k != "state"},
    )
    assert executor.execute("transcribe.process", INPUT, step="notes", request_id="r")["state"] == (
        "completed"
    )


def test_missing_requested_output_type_is_response_invalid(tmp_path):
    executor = IntegrationExecutor(
        compiled(tmp_path),
        reviewed=True,
        connector_client=lambda **_: {
            **COMPLETED,
            "output": {**COMPLETED["output"], "outputs": {}},
        },
    )
    with pytest.raises(IntegrationError, match="transcript") as raised:
        executor.execute("transcribe.process", INPUT, step="notes", request_id="r")
    assert raised.value.code == "integration.response_invalid"


# Polling (D3, D4)


def test_poll_backoff_floor_and_cap():
    clock = Clock()
    waits = []
    answers = iter(
        [
            {"state": "pending", "retry_after_seconds": 0},
            {"state": "pending", "retry_after_seconds": 2},
            {"state": "pending", "retry_after_seconds": 300},
            {"state": "pending", "retry_after_seconds": 10},
            {"state": "completed", "ok": True, "status": 200},
        ]
    )

    def sleep(seconds):
        waits.append(seconds)
        clock.sleep(seconds)

    recorded = []
    outcome, response = pending.poll(
        {"state": "pending", "retry_after_seconds": None},
        call=lambda: next(answers),
        cancel=lambda: {"state": "cancelled"},
        # The budget runs out 7 s into the last wait: it is clipped and the
        # deadline is honoured without one more request.
        deadline=clock.now + 10 + 10 + 5 + 60 + 7,
        stop=lambda: False,
        record=recorded.append,
        clock=clock,
        sleep=sleep,
    )
    assert outcome == "timeout"
    assert waits == [10, 10, 5, 60, 7]
    assert [a.kind for a in recorded] == ["call"] * 4 + ["cancel"]
    assert response["retry_after_seconds"] == 10


def test_pending_twice_then_completed_is_one_request_id(tmp_path):
    value = compiled(tmp_path)
    answers = iter([PENDING, PENDING, COMPLETED])
    calls = []

    def proxy(**call):
        calls.append(call)
        return dict(next(answers))

    result = broker(tmp_path, value, connector_client=proxy).execute(
        "transcribe.process", INPUT, step="notes"
    )
    assert result["output"]["outputs"]["transcript"]["text"] == "hello"
    assert len(calls) == 3 and len({c["request_id"] for c in calls}) == 1
    call = journal(tmp_path)["calls"][result["receipt"]]
    assert call["status"] == "confirmed" and call["managed"] is True
    assert [a["state"] for a in call["attempts"]] == ["pending", "pending", "completed"]
    assert [a["kind"] for a in call["attempts"]] == ["call"] * 3
    assert [e["attempt"] for e in events(tmp_path, "integration.pending")] == [1, 2]
    (completed,) = events(tmp_path, "integration.completed")
    assert completed["minutes"] == 1.02
    assert [e["event_type"] for e in events(tmp_path)][-3:] == [
        "integration.pending",
        "integration.pending",
        "integration.completed",
    ]


def _crashed_journal(tmp_path, value, *, revision=None):
    """What a crash before the first acknowledgement leaves behind."""
    directory = tmp_path / "broker"
    directory.mkdir()
    fingerprint = "f" * 64
    (directory / "journal.json").write_text(
        json.dumps(
            {
                "calls": {
                    fingerprint: {
                        "capability": "transcribe.process",
                        "step": "notes",
                        "invocation": "old",
                        "sequence": 1,
                        "as": None,
                        "status": "pending",
                        "proposal_sha256": fingerprint,
                        "request": INPUT,
                        "managed": True,
                        "attempts": [],
                    }
                },
                "references": {},
                "events": [],
            }
        )
    )
    if revision is not None:
        value["workflow_revision"] = revision
    return fingerprint


@pytest.mark.parametrize("revision", [None, "other-revision"])
def test_resume_after_crash_reuses_the_request_id(tmp_path, revision):
    value = compiled(tmp_path)
    fingerprint = _crashed_journal(tmp_path, value, revision=revision)
    calls = []

    def proxy(**call):
        calls.append(call)
        return dict(COMPLETED)

    result = broker(tmp_path, value, connector_client=proxy).execute(
        "transcribe.process", INPUT, step="notes"
    )
    assert result["receipt"] == fingerprint
    assert [c["request_id"] for c in calls] == [fingerprint]
    state = journal(tmp_path)
    assert list(state["calls"]) == [fingerprint]
    assert state["calls"][fingerprint]["status"] == "confirmed"
    kinds = [e["event_type"] for e in state["events"]]
    assert "integration.proposed" not in kinds
    assert kinds[0] == "integration.pending" and state["events"][0]["resumed"] is True


def test_failed_completion_returns_failure_category_to_the_agent(tmp_path):
    value = compiled(tmp_path)
    failed = {
        "state": "completed",
        "ok": False,
        "status": 404,
        "failure_category": "input_invalid",
        "output": {"error": "integration.attachment_not_found", "detail": "no att_01"},
        "audit": {},
    }
    result = broker(tmp_path, value, connector_client=lambda **_: dict(failed)).execute(
        "transcribe.process", INPUT, step="notes"
    )
    assert result["output"]["error"] == "integration.attachment_not_found"
    assert result["failure_category"] == "input_invalid"
    assert journal(tmp_path)["calls"][result["receipt"]]["status"] == "failed"
    (event,) = events(tmp_path, "integration.failed")
    assert event["failure_category"] == "input_invalid"
    assert event["error"] == "integration.attachment_not_found"


def test_timeout_cancels_once_and_raises_integration_timeout(tmp_path):
    value = compiled(tmp_path)
    clock = Clock()
    cancels = []
    runtime = broker(
        tmp_path,
        value,
        connector_client=lambda **_: dict(PENDING),
        connector_cancel=lambda **kw: cancels.append(kw) or {"state": "cancelled"},
        deadline=clock.now + 60,
        timeout_seconds=60,
        clock=clock,
    )
    runtime.sleep = clock.sleep
    with pytest.raises(IntegrationError, match="step timeout") as raised:
        runtime.execute("transcribe.process", INPUT, step="notes")
    assert raised.value.code == "integration.timeout"
    assert raised.value.category == "timeout"
    (call,) = journal(tmp_path)["calls"].values()
    assert cancels == [{"request_id": call["proposal_sha256"]}]
    assert call["status"] == "cancelled" and call["receipt_state"] == "cancelled"
    assert call["attempts"][-1]["kind"] == "cancel"
    assert call["attempts"][-1]["state"] == "cancelled"
    assert call["result"] == {
        "ok": False,
        "error": "integration.timeout",
        "timeout_seconds": 60,
        "receipt": call["proposal_sha256"],
    }
    (event,) = events(tmp_path, "integration.cancelled")
    assert event["reason"] == "timeout" and event["level"] == "warning"
    assert clock.now <= 1000 + 60 + pending.POLL_FLOOR_SECONDS


def test_cancel_failure_is_journalled_and_timeout_still_raised(tmp_path):
    value = compiled(tmp_path)
    clock = Clock()

    def cancel(**_):
        raise CoreError("core_rejected", False, "unknown request")

    runtime = broker(
        tmp_path,
        value,
        connector_client=lambda **_: dict(PENDING),
        connector_cancel=cancel,
        deadline=clock.now + 20,
        clock=clock,
    )
    runtime.sleep = clock.sleep
    with pytest.raises(IntegrationError) as raised:
        runtime.execute("transcribe.process", INPUT, step="notes")
    assert raised.value.code == "integration.timeout"
    (call,) = journal(tmp_path)["calls"].values()
    assert call["attempts"][-1]["kind"] == "cancel"
    assert "core_rejected" in call["attempts"][-1]["error"]
    assert call["status"] == "cancelled"


def test_run_cancelled_predicate_stops_polling(tmp_path):
    value = compiled(tmp_path)
    gone = []
    cancels = []

    def proxy(**_):
        gone.append(True)
        return dict(PENDING)

    runtime = broker(
        tmp_path,
        value,
        connector_client=proxy,
        connector_cancel=lambda **kw: cancels.append(kw) or {},
        run_cancelled=lambda: bool(gone),
    )
    with pytest.raises(IntegrationError) as raised:
        runtime.execute("transcribe.process", INPUT, step="notes")
    assert raised.value.code == "integration.cancelled"
    assert raised.value.category == "cancelled"
    assert workflow_failure_category(raised.value) == "agent_cancelled"
    assert len(cancels) == 1 and len(gone) == 1
    (call,) = journal(tmp_path)["calls"].values()
    assert call["status"] == "cancelled"
    assert call["result"]["output"] == {"error": "integration.cancelled"}
    (event,) = events(tmp_path, "integration.cancelled")
    assert event["reason"] == "run_cancelled"


def test_api_cancelled_state_is_recorded_as_cancelled(tmp_path):
    value = compiled(tmp_path)
    answers = iter(
        [PENDING, {"state": "cancelled", "ok": False, "status": 409, "output": {}, "audit": {}}]
    )
    runtime = broker(
        tmp_path,
        value,
        connector_client=lambda **_: dict(next(answers)),
        connector_cancel=lambda **_: pytest.fail("cancel sent"),
    )
    with pytest.raises(IntegrationError) as raised:
        runtime.execute("transcribe.process", INPUT, step="notes")
    assert raised.value.code == "integration.cancelled"
    (call,) = journal(tmp_path)["calls"].values()
    assert call["status"] == "cancelled"
    assert [a["kind"] for a in call["attempts"]] == ["call", "call"]
    assert events(tmp_path, "integration.cancelled")[0]["reason"] == "api"


def test_retryable_core_error_mid_poll_is_one_more_attempt(tmp_path):
    value = compiled(tmp_path)
    answers = iter([PENDING, CoreError("core_unavailable", True), COMPLETED])

    def proxy(**_):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    result = broker(tmp_path, value, connector_client=proxy).execute(
        "transcribe.process", INPUT, step="notes"
    )
    call = journal(tmp_path)["calls"][result["receipt"]]
    assert [a["state"] for a in call["attempts"]] == ["pending", None, "completed"]
    assert "core_unavailable" in call["attempts"][1]["error"]
    assert call["status"] == "confirmed"


def test_non_retryable_core_error_leaves_the_call_uncertain(tmp_path):
    value = compiled(tmp_path)
    answers = iter([PENDING, CoreError("core_rejected", False, "gone")])

    def proxy(**_):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    with pytest.raises(CoreError):
        broker(tmp_path, value, connector_client=proxy).execute(
            "transcribe.process", INPUT, step="notes"
        )
    (call,) = journal(tmp_path)["calls"].values()
    assert call["status"] == "uncertain"


def test_two_proposals_are_two_request_ids(tmp_path):
    value = compiled(tmp_path)
    calls = []

    def proxy(**call):
        calls.append(call["request_id"])
        return dict(COMPLETED)

    runtime = broker(tmp_path, value, connector_client=proxy)
    runtime.execute("transcribe.process", INPUT, step="notes")
    runtime.execute("transcribe.process", {**INPUT, "attachment": "att_02"}, step="notes")
    assert len(set(calls)) == 2
    # The same proposal again replays the receipt instead of a second job.
    replay = runtime.execute("transcribe.process", INPUT, step="notes")
    assert replay["replayed"] is True and len(calls) == 2


def test_dataset_export_check_passes_with_attempts(tmp_path):
    value = compiled(tmp_path)
    answers = iter([PENDING, PENDING, COMPLETED])
    broker(tmp_path, value, connector_client=lambda **_: dict(next(answers))).execute(
        "transcribe.process", INPUT, step="notes"
    )
    record = run_records.calls_record(journal(tmp_path))
    run = tmp_path / "run"
    run.mkdir()
    (run / "calls.json").write_text(json.dumps(record))
    for name in ("run.json", "manifest.json", "effects.json", "policy.json"):
        (run / name).write_text("{}")
    paths = [path.name for path in run.iterdir()]
    assert dataset.completeness(paths, {"completed_steps": []})["complete"] is True
    decoded = dataset._decode("calls.json", (run / "calls.json").read_bytes())
    assert len(decoded["calls"][0]["attempts"]) == 3


# Runner client (D7)


def test_connector_cancel_posts_lease_and_request_id():
    requests = []
    statuses = iter([200, 404, 409])

    def send(request):
        requests.append((request.url.path, json.loads(request.content)))
        status = next(statuses)
        if status == 200:
            return httpx.Response(200, json={"state": "cancelled", "request_id": "a" * 64})
        return httpx.Response(status, json={"detail": "no such call"})

    client = CoreClient(
        "https://example.test",
        "invocation",
        "bootstrap",
        "workflow",
        transport=httpx.MockTransport(send),
    )
    assert client.workflow_connector_cancel("lease", request_id="a" * 64)["state"] == "cancelled"
    assert requests[0][0].endswith("/connector-call/cancel")
    assert requests[0][1] == {"lease_token": "lease", "request_id": "a" * 64}
    with pytest.raises(CoreError) as missing:
        client.workflow_connector_cancel("lease", request_id="a" * 64)
    assert missing.value.category == "core_rejected" and not missing.value.retryable
    with pytest.raises(CoreError) as synchronous:
        client.workflow_connector_cancel("lease", request_id="a" * 64)
    assert synchronous.value.category == "core_conflict"
    assert len(requests) == 3


def test_doctor_reports_a_managed_connection_without_probing(tmp_path):
    from outcomeci.broker.executor import doctor

    report = doctor(compiled(tmp_path), connectivity=True)
    assert report["checks"] == [
        {
            "check": "credential_reference",
            "connection": "transcribe",
            "status": "managed",
            "accepts": ["none"],
        }
    ]
    assert report["ok"] is True
