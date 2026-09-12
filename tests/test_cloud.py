from __future__ import annotations

import json
import stat
from pathlib import Path

from outcomeci import cloud
from outcomeci.repository import initialize


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
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
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
    assert ".outcomeci/constitution.md" in captured["body"]["files"]


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
