"""How one v1 agent step runs: trigger input, output repair, receipts, transcripts."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml
from lowered import email_payload

from outcomeci.runtime import engine as local
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import compile_workflow

WORKFLOW = {
    "apiVersion": "outcomeci.workflow/v1",
    "name": "summarize-email",
    "trigger": "email",
    "steps": [
        {
            "summarize": {
                "reason": "Summarize the email in one sentence.",
                "from": "trigger",
                "returns": {"summary": "string"},
            }
        }
    ],
}


def _resolver(reference: str) -> str:
    raise AssertionError(f"no credential should be resolved, asked for {reference}")


OPTIONS = local.ExecutionOptions(credential_resolver=_resolver)


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(WORKFLOW, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(local, "serve_capability", lambda *args, **kwargs: nullcontext({}))
    return path


def _result_path(kwargs) -> Path:
    return next(item for item in kwargs["writable_paths"] if item.name == "outputs.json")


def test_an_invalid_trigger_never_reaches_an_agent(tmp_path, workflow, monkeypatch):
    invoked = []
    monkeypatch.setattr(local, "_execute", lambda *args, **kwargs: invoked.append(True))
    with pytest.raises(ExecutionError, match="contract failed"):
        local.trigger(tmp_path, workflow, "email", {"subject": "Incomplete email"}, options=OPTIONS)
    assert invoked == []


@pytest.mark.parametrize(("trigger", "limit"), [("email", "1 MiB"), ("webhook", "2 MiB")])
def test_an_oversized_payload_names_its_trigger_limit(tmp_path, monkeypatch, trigger, limit):
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump({**WORKFLOW, "trigger": trigger}), encoding="utf-8")
    with pytest.raises(ExecutionError, match=f"exceeds the {limit} local limit"):
        local.trigger(tmp_path, path, trigger, {"body": "x" * (3 * 1024 * 1024)}, options=OPTIONS)


def test_the_run_keeps_the_exact_trigger_payload(tmp_path, workflow, monkeypatch):
    payload = email_payload()
    monkeypatch.setattr(local, "_execute", lambda _root, _config, state, **kwargs: state)
    state = local.trigger(tmp_path, workflow, "email", payload, options=OPTIONS)
    payload["subject"] = "Changed later"
    assert state["trigger"]["value"]["subject"] == "Receipt arrived"
    assert state["step"] == "summarize"


def test_a_step_needs_a_scoped_credential_resolver(tmp_path, workflow):
    with pytest.raises(ExecutionError, match="scoped credential resolver"):
        local.trigger(tmp_path, workflow, "email", email_payload())


def test_an_invalid_result_is_repaired_once_without_capabilities(tmp_path, workflow, monkeypatch):
    calls, events = [], []
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )

    def invoke(*args, **kwargs):
        calls.append(kwargs)
        _result_path(kwargs).write_text(
            '{"summary": "A receipt arrived."}' if len(calls) == 2 else '{"summary": 3}'
        )
        return "complete"

    monkeypatch.setattr(local, "invoke", invoke)
    state = local.trigger(
        tmp_path,
        workflow,
        "email",
        email_payload(),
        options=local.ExecutionOptions(credential_resolver=_resolver, event_sink=events.append),
    )

    assert state["status"] == "completed"
    assert len(calls) == 2
    assert calls[1]["extra_env"] == {}
    assert [item["event_type"] for item in events] == [
        "artifact.repair_started",
        "artifact.repair_completed",
    ]


def test_repair_runs_without_capabilities_or_connection_secrets(tmp_path, workflow, monkeypatch):
    compiled = compile_workflow(workflow)
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    (outcome / "summarize").mkdir(parents=True)
    calls = []

    def invoke(*args, **kwargs):
        calls.append((args, kwargs))
        (outcome / "summarize/outputs.json").write_text('{"summary": "ok"}')
        return "repaired"

    monkeypatch.setattr(local, "invoke", invoke)
    summary = local._repair_outputs(
        tmp_path,
        compiled,
        outcome,
        "summarize",
        "codex",
        None,
        ExecutionError("missing required output"),
        [outcome / "summarize/outputs.json"],
        {"SLACK_TOKEN"},
        container_isolated=True,
    )
    assert summary == "repaired"
    assert calls[0][1]["extra_env"] == {}
    assert calls[0][1]["excluded_env"] == {"SLACK_TOKEN"}
    assert calls[0][1]["container_isolated"] is True
    assert "must not be repeated" in calls[0][0][2]


def test_transcripts_are_kept_when_the_result_never_validates(tmp_path, workflow, monkeypatch):
    captured = []

    def transcripts(*args, **kwargs):
        captured.append(args)
        return {"usage_records": 0, "files": [], "usage": []}

    def invoke(*args, **kwargs):
        _result_path(kwargs).write_text('{"summary": 3}')
        return "complete"

    monkeypatch.setattr(local, "_transcripts", transcripts)
    monkeypatch.setattr(local, "invoke", invoke)
    with pytest.raises(ExecutionError):
        local.trigger(tmp_path, workflow, "email", email_payload(), options=OPTIONS)

    assert len(captured) == 1
    assert captured[0][2] == "summarize"


def test_effect_receipts_keep_no_request_or_response_content(tmp_path: Path) -> None:
    broker = tmp_path / ".outcomeci/.broker/run-1"
    broker.mkdir(parents=True)
    (broker / "journal.json").write_text(
        json.dumps(
            {
                "calls": {
                    "digest": {
                        "step": "notify",
                        "capability": "slack.post",
                        "proposal_sha256": "a" * 64,
                        "status": "confirmed",
                        "request": {"authorization": "Bearer secret", "body": "private"},
                        "result": {
                            "ok": True,
                            "status": 200,
                            "output": {"result": {"ok": True, "channel": "D123456789"}},
                        },
                    }
                }
            }
        )
    )
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    outcome.mkdir(parents=True)
    receipt = local._write_effect_receipts(tmp_path, outcome, "run-1", "notify")
    assert json.loads(receipt.read_text()) == {
        "schema_version": "1",
        "step": "notify",
        "effects": [
            {
                "step": "notify",
                "capability": "slack.post",
                "proposal_sha256": "a" * 64,
                "status": "confirmed",
                "ok": True,
                "http_status": 200,
            }
        ],
    }
    assert "secret" not in receipt.read_text()
    assert "D123456789" not in receipt.read_text()
