from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from outcomeci import debug
from outcomeci.process import ExecutionError

COMPILED = {"triggers": {"daily": {"type": "cron"}, "go": {"type": "manual"}}}


def _lease(**overrides):
    value = {
        "lease_id": "lease-1",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "values": {"slack/bot-token": {"secrets": {"value": "xoxb-secret"}}},
    }
    value.update(overrides)
    return value


def test_synthesizes_a_cron_payload_and_runs_with_the_leased_resolver(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    complete = mock.Mock()
    monkeypatch.setattr(debug, "complete_debug_lease", complete)
    captured = {}

    def trigger(root, config, name, payload, **options):
        captured.update(name=name, payload=payload, options=options)
        assert options["credential_resolver"]("vault:slack/bot-token")["secrets"]["value"] == (
            "xoxb-secret"
        )
        assert options["execution_backend"] == "outcomeci"
        assert options["_container_isolated"] is False
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        result = debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="daily"
        )

    assert result == {"run_id": "run-1"}
    assert captured["name"] == "daily"
    assert captured["payload"]["type"] == "cron"
    assert captured["payload"]["schema_version"] == "outcomeci.trigger.cron/v1"
    assert captured["payload"]["trigger_name"] == "daily"
    complete.assert_not_called()


def test_manual_trigger_synthesizes_an_empty_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, **options):
        assert payload == {}
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="go"
        )


def test_unsynthesizable_trigger_type_requires_a_payload_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        debug,
        "compile_workflow",
        lambda config: {"triggers": {"inbound": {"type": "webhook.received"}}},
    )
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())

    with pytest.raises(ExecutionError, match="pass --payload"):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            trigger_name="inbound",
        )


def test_payload_file_overrides_synthesis(monkeypatch, tmp_path):
    monkeypatch.setattr(
        debug,
        "compile_workflow",
        lambda config: {"triggers": {"inbound": {"type": "webhook.received"}}},
    )
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"subject": "hello"}))
    captured = {}

    def trigger(root, config, name, payload, **options):
        captured["payload"] = payload
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            trigger_name="inbound",
            payload_path=payload_file,
        )
    assert captured["payload"] == {"subject": "hello"}


def test_without_trigger_or_run_raises_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())

    with pytest.raises(ExecutionError, match="--trigger"):
        debug.run(tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1")


def test_run_mode_replays_the_real_claimed_invocation_and_reports_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(
        debug,
        "issue_debug_lease",
        lambda *a, **k: _lease(
            trigger_name="daily", trigger_type="cron", input={"type": "cron", "scheduled_at": "x"}
        ),
    )
    complete = mock.Mock()
    monkeypatch.setattr(debug, "complete_debug_lease", complete)
    captured = {}

    def trigger(root, config, name, payload, **options):
        captured.update(name=name, payload=payload)
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            invocation_id="inv-1",
        )

    assert captured["name"] == "daily"
    assert captured["payload"] == {"type": "cron", "scheduled_at": "x"}
    complete.assert_called_once_with("workspace_1", "workflow_1", "inv-1", "completed")


def test_run_mode_reports_failure_and_still_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(
        debug,
        "issue_debug_lease",
        lambda *a, **k: _lease(trigger_name="daily", trigger_type="cron", input={}),
    )
    complete = mock.Mock()
    monkeypatch.setattr(debug, "complete_debug_lease", complete)

    def trigger(root, config, name, payload, **options):
        raise ExecutionError("boom")

    with (
        mock.patch("outcomeci.local.trigger", side_effect=trigger),
        pytest.raises(ExecutionError, match="boom"),
    ):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            invocation_id="inv-1",
        )

    complete.assert_called_once_with("workspace_1", "workflow_1", "inv-1", "failed")


def test_auto_continue_drives_through_ready_phases(monkeypatch, tmp_path):
    compiled = {
        "triggers": {"daily": {"type": "cron"}},
        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
    }
    monkeypatch.setattr(debug, "compile_workflow", lambda config: compiled)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())
    continue_calls = []

    def trigger(root, config, name, payload, **options):
        return {
            "run_id": "run-1",
            "status": "awaiting_confirmation",
            "completed_phases": ["resolve_analytics"],
            "ready_phases": ["notify"],
        }

    def continue_run(root, config, run_id, *, approve, **options):
        continue_calls.append((run_id, approve, options))
        return {
            "run_id": run_id,
            "status": "completed",
            "completed_phases": ["resolve_analytics", "notify"],
        }

    with (
        mock.patch("outcomeci.local.trigger", side_effect=trigger),
        mock.patch("outcomeci.local.continue_run", side_effect=continue_run),
    ):
        result = debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            trigger_name="daily",
            auto_continue=True,
        )

    assert result["completed_phases"] == ["resolve_analytics", "notify"]
    assert len(continue_calls) == 1
    assert continue_calls[0][0:2] == ("run-1", True)
    assert continue_calls[0][2]["execution_backend"] == "outcomeci"
    assert continue_calls[0][2]["_container_isolated"] is False


def test_without_auto_continue_stops_after_the_first_phase(monkeypatch, tmp_path):
    compiled = {
        "triggers": {"daily": {"type": "cron"}},
        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
    }
    monkeypatch.setattr(debug, "compile_workflow", lambda config: compiled)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, **options):
        return {
            "run_id": "run-1",
            "status": "awaiting_confirmation",
            "completed_phases": ["resolve_analytics"],
            "ready_phases": ["notify"],
        }

    with (
        mock.patch("outcomeci.local.trigger", side_effect=trigger),
        mock.patch(
            "outcomeci.local.continue_run",
            side_effect=AssertionError("continue_run should not be called"),
        ),
    ):
        result = debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="daily"
        )

    assert result["completed_phases"] == ["resolve_analytics"]


def test_resolver_rejects_a_reference_missing_from_the_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, **options):
        with pytest.raises(ExecutionError, match="not granted"):
            options["credential_resolver"]("vault:unknown/path")
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="go"
        )


def test_resolver_rejects_an_expired_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    expired = _lease(expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: expired)
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, **options):
        with pytest.raises(ExecutionError, match="expired"):
            options["credential_resolver"]("vault:slack/bot-token")
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="go"
        )
