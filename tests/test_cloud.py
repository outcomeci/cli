from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx

from outcomeci import cloud
from outcomeci.repository import initialize


def test_request_builds_the_httpx_call_and_parses_a_json_response(monkeypatch) -> None:
    captured = {}

    def fake_request(method, url, *, json=None, headers=None, timeout=None, follow_redirects=None):
        captured.update(
            method=method, url=url, json=json, headers=headers, follow_redirects=follow_redirects
        )
        return httpx.Response(201, json={"id": "entry-1"})

    monkeypatch.setattr(cloud.httpx, "request", fake_request)

    status, value = cloud._request(
        "https://api.outcomeci.test/",
        "/workspaces/w1/vault/secrets",
        method="POST",
        body={"path": "x"},
        token="tok",
    )

    assert status == 201
    assert value == {"id": "entry-1"}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.outcomeci.test/v1/workspaces/w1/vault/secrets"
    assert captured["json"] == {"path": "x"}
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["follow_redirects"] is True


def test_request_tolerates_a_non_json_error_body(monkeypatch) -> None:
    def fake_request(method, url, **kwargs):
        return httpx.Response(500, text="upstream exploded")

    monkeypatch.setattr(cloud.httpx, "request", fake_request)

    status, value = cloud._request("https://api.outcomeci.test", "/x")

    assert status == 500
    assert value == {}


def test_request_wraps_a_network_failure(monkeypatch) -> None:
    def fake_request(method, url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cloud.httpx, "request", fake_request)

    try:
        cloud._request("https://api.outcomeci.test", "/x")
        raise AssertionError("expected ExecutionError")
    except cloud.ExecutionError as exc:
        assert "could not reach OutcomeCI Cloud" in str(exc)


def test_device_login_persists_owner_only_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    responses = iter(
        [
            (
                201,
                {
                    "device_code": "device-secret",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "http://localhost:3000/auth/cli?code=ABCD-EFGH",
                    "expires_in": 60,
                    "interval": 1,
                },
            ),
            (428, {"detail": "Authorization pending"}),
            (200, {"access_token": "access", "refresh_token": "refresh", "token_type": "bearer"}),
        ]
    )
    monkeypatch.setattr(cloud, "_request", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(cloud.time, "sleep", lambda _: None)
    monkeypatch.setattr(cloud.webbrowser, "open", lambda _: True)
    result = cloud.login("http://localhost:8000")
    path = cloud.credentials_path()
    assert result["authenticated"] is True
    assert json.loads(path.read_text())["refresh_token"] == "refresh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_workspace_key_login_persists_non_refreshing_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    result = cloud.login_with_key("http://localhost:8000/", "oci_" + "a" * 40)
    stored = json.loads(cloud.credentials_path().read_text())
    assert result == {
        "authenticated": True,
        "api_url": "http://localhost:8000",
        "credential_type": "workspace_key",
    }
    assert stored["credential_type"] == "workspace_key"
    assert stored["access_token"].startswith("oci_")


def test_revoked_workspace_key_is_not_sent_to_refresh_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path))
    cloud._write_credentials(
        {
            "api_url": "https://api.outcomeci.com",
            "access_token": "oci_" + "a" * 40,
            "credential_type": "workspace_key",
        }
    )
    calls = []
    monkeypatch.setattr(
        cloud, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or (401, {})
    )
    try:
        cloud._authorized_request("/workspaces/workspace_1/workflow-revisions")
    except Exception as exc:
        assert "invalid or revoked" in str(exc)
    else:
        raise AssertionError("revoked workspace key should fail")
    assert len(calls) == 1


def test_sync_validates_and_sends_explicit_create_mode(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path)
    workflow = tmp_path / "outcome.yml"
    (tmp_path / ".outcomeci/vault.enc").write_text("encrypted-local-vault")
    captured = {}

    def request(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return 201, {
            "workflow_id": "00000000-0000-0000-0000-000000000001",
            "name": "code-outcome",
            "revision": 1,
        }

    monkeypatch.setattr(cloud, "_authorized_request", request)
    result = cloud.sync_workflow(workflow, "workspace_1", "code-outcome", "create")
    assert result["revision"] == 1
    assert captured["path"] == "/workspaces/workspace_1/workflow-revisions"
    assert captured["body"]["mode"] == "create"
    assert captured["body"]["content_type"] == "yaml"
    assert "apiVersion: outcomeci.workflow/v1" in captured["body"]["content"]
    assert ".outcomeci/instructions/investigate.md" in captured["body"]["files"]
    assert ".outcomeci/vault.enc" not in captured["body"]["files"]


def test_get_workflow_reads_the_latest_revision(monkeypatch) -> None:
    captured = {}

    def request(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return 200, {
            "workflow_id": "00000000-0000-0000-0000-000000000001",
            "name": "code-outcome",
            "revision": 3,
            "content": "apiVersion: outcomeci.workflow/v1alpha1\n",
            "content_sha256": "deadbeef",
            "content_type": "yaml",
            "source_filename": "outcome.yml",
            "created_at": "2026-09-23T00:00:00Z",
        }

    monkeypatch.setattr(cloud, "_authorized_request", request)
    result = cloud.get_workflow("workspace_1", "workflow_1")
    assert result["revision"] == 3
    assert captured["path"] == "/workspaces/workspace_1/workflow-revisions/workflow_1/latest"
    assert captured["method"] == "GET"


def test_get_workflow_raises_when_not_found(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud, "_authorized_request", lambda *a, **k: (404, {"detail": "Workflow not found"})
    )
    try:
        cloud.get_workflow("workspace_1", "workflow_1")
    except Exception as exc:
        assert "Workflow not found" in str(exc)
    else:
        raise AssertionError("expected an ExecutionError")


def test_issue_debug_lease_posts_the_optional_invocation_id(monkeypatch) -> None:
    captured = {}

    def request(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return 200, {"lease_id": "lease-1", "expires_at": "2026-09-19T18:00:00+00:00", "values": {}}

    monkeypatch.setattr(cloud, "_authorized_request", request)
    result = cloud.issue_debug_lease("workspace_1", "workflow_1")
    assert result["lease_id"] == "lease-1"
    assert captured["path"] == "/workspaces/workspace_1/workflows/workflow_1/debug-lease"
    assert captured["body"] == {"invocation_id": None, "ttl_seconds": 600}


def test_issue_debug_lease_raises_the_server_detail_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud, "_authorized_request", lambda *a, **k: (409, {"detail": "not queued"})
    )
    try:
        cloud.issue_debug_lease("workspace_1", "workflow_1", invocation_id="inv-1")
    except Exception as exc:
        assert "not queued" in str(exc)
    else:
        raise AssertionError("expected an ExecutionError")


def test_complete_debug_lease_posts_status(monkeypatch) -> None:
    captured = {}

    def request(path, *, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return 200, {"completed": True}

    monkeypatch.setattr(cloud, "_authorized_request", request)
    cloud.complete_debug_lease("workspace_1", "workflow_1", "inv-1", "failed")
    assert (
        captured["path"]
        == "/workspaces/workspace_1/workflows/workflow_1/debug-lease/inv-1/complete"
    )
    assert captured["body"] == {"status": "failed"}


def test_logout_revokes_before_removing_local_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path))
    cloud._write_credentials(
        {
            "api_url": "https://api.outcomeci.com",
            "access_token": "access",
            "refresh_token": "refresh",
        }
    )
    calls = []
    monkeypatch.setattr(
        cloud, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or (200, {})
    )
    assert cloud.logout() == {"authenticated": False}
    assert calls[0][0][1] == "/auth/revoke"
    assert not cloud.credentials_path().exists()


def _capture(monkeypatch, status=200, value=None):
    calls = []

    def request(path, *, method="GET", body=None):
        calls.append({"path": path, "method": method, "body": body})
        return status, {} if value is None else value

    monkeypatch.setattr(cloud, "_authorized_request", request)
    return calls


def test_issue_debug_lease_asks_for_the_agent_credential(monkeypatch) -> None:
    calls = _capture(monkeypatch, value={"lease_id": "l1"})

    cloud.issue_debug_lease("w1", "wf1", ttl_seconds=3600, agent_provider="codex")

    assert calls == [
        {
            "path": "/workspaces/w1/workflows/wf1/debug-lease",
            "method": "POST",
            "body": {"invocation_id": None, "ttl_seconds": 3600, "agent_provider": "codex"},
        }
    ]


def test_renew_debug_agent_lease_sends_the_token_and_ttl(monkeypatch) -> None:
    calls = _capture(monkeypatch, value={"expires_at": "2026-09-27T20:00:00+00:00"})

    cloud.renew_debug_agent_lease("w1", "wf1", "job-1", "agent-token", 900)

    assert calls[0]["path"] == "/workspaces/w1/workflows/wf1/debug-lease/agent/job-1/renew"
    assert calls[0]["body"] == {"token": "agent-token", "ttl_seconds": 900}


def test_complete_debug_agent_lease_sends_the_version_only_with_a_writeback(
    monkeypatch,
) -> None:
    calls = _capture(monkeypatch, value={"completed": True})

    cloud.complete_debug_agent_lease("w1", "wf1", "job-1", "agent-token", "failed")
    cloud.complete_debug_agent_lease(
        "w1",
        "wf1",
        "job-1",
        "agent-token",
        "completed",
        agent_credential={"tokens": {}},
        expected_credential_version=4,
    )

    assert calls[0]["path"] == "/workspaces/w1/workflows/wf1/debug-lease/agent/job-1/complete"
    assert calls[0]["body"] == {"token": "agent-token", "status": "failed"}
    assert calls[1]["body"] == {
        "token": "agent-token",
        "status": "completed",
        "agent_credential": {"tokens": {}},
        "expected_credential_version": 4,
    }


def test_cloud_errors_say_whether_they_are_worth_retrying(monkeypatch) -> None:
    for status, transient in ((409, False), (404, False), (429, True), (503, True)):
        _capture(monkeypatch, status=status, value={"detail": "nope"})
        try:
            cloud.renew_debug_agent_lease("w1", "wf1", "job-1", "t", 900)
            raise AssertionError("expected CloudRequestError")
        except cloud.CloudRequestError as exc:
            assert (exc.status, exc.transient) == (status, transient)

    def unreachable(method, url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cloud.httpx, "request", unreachable)
    try:
        cloud._request("https://api.outcomeci.test", "/x")
        raise AssertionError("expected CloudRequestError")
    except cloud.CloudRequestError as exc:
        assert exc.status is None and exc.transient
