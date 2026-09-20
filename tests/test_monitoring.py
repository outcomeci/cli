from __future__ import annotations

from unittest import mock

import pytest

from outcomeci.cloud_runner import monitoring


@pytest.fixture(autouse=True)
def reset_monitoring_state():
    monitoring._enabled = False
    yield
    monitoring._enabled = False


def test_init_is_a_noop_without_a_dsn(monkeypatch):
    monkeypatch.delenv("OUTCOMECI_RUNNER_SENTRY_DSN", raising=False)
    with mock.patch("sentry_sdk.init") as init:
        monitoring.init_exception_monitoring()
    init.assert_not_called()
    assert monitoring._enabled is False


def test_init_configures_sentry_with_scrubbing_when_a_dsn_is_set(monkeypatch):
    monkeypatch.setenv("OUTCOMECI_RUNNER_SENTRY_DSN", "https://key@sentry.example.com/1")
    monkeypatch.setenv("OUTCOMECI_ENV", "staging")
    with mock.patch("sentry_sdk.init") as init:
        monitoring.init_exception_monitoring()
    init.assert_called_once()
    kwargs = init.call_args.kwargs
    assert kwargs["dsn"] == "https://key@sentry.example.com/1"
    assert kwargs["environment"] == "staging"
    assert kwargs["send_default_pii"] is False
    assert kwargs["include_local_variables"] is False
    assert kwargs["max_breadcrumbs"] == 0
    assert kwargs["before_send"] is monitoring._strip_sensitive_context
    assert monitoring._enabled is True


def test_capture_exception_is_a_noop_when_not_enabled():
    with mock.patch("sentry_sdk.capture_exception") as capture:
        monitoring.capture_exception(ValueError("boom"))
    capture.assert_not_called()


def test_capture_exception_reports_once_enabled(monkeypatch):
    monkeypatch.setenv("OUTCOMECI_RUNNER_SENTRY_DSN", "https://key@sentry.example.com/1")
    with mock.patch("sentry_sdk.init"):
        monitoring.init_exception_monitoring()
    error = ValueError("boom")
    with mock.patch("sentry_sdk.capture_exception") as capture:
        monitoring.capture_exception(error)
    capture.assert_called_once_with(error)


def test_capture_exception_tags_the_scope_when_given_context(monkeypatch):
    monkeypatch.setenv("OUTCOMECI_RUNNER_SENTRY_DSN", "https://key@sentry.example.com/1")
    with mock.patch("sentry_sdk.init"):
        monitoring.init_exception_monitoring()
    error = ValueError("boom")
    scope = mock.Mock()
    scope_cm = mock.MagicMock()
    scope_cm.__enter__.return_value = scope
    with (
        mock.patch("sentry_sdk.push_scope", return_value=scope_cm) as push_scope,
        mock.patch("sentry_sdk.capture_exception") as capture,
    ):
        monitoring.capture_exception(error, category="core_conflict")
    push_scope.assert_called_once()
    scope.set_tag.assert_called_once_with("category", "core_conflict")
    capture.assert_called_once_with(error)


def test_strip_sensitive_context_removes_request_user_and_breadcrumbs():
    event = {
        "request": {"data": "customer content"},
        "user": {"id": "workspace_1"},
        "breadcrumbs": [{"message": "agent output"}],
        "exception": {"values": []},
    }
    scrubbed = monitoring._strip_sensitive_context(event, {})
    assert "request" not in scrubbed
    assert "user" not in scrubbed
    assert "breadcrumbs" not in scrubbed
    assert scrubbed["exception"] == {"values": []}
