"""A provider that rotates a secret revokes the old one: every run mode saves
the new one to the Vault the credential came from."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from outcomeci.cloud import CloudRequestError
from outcomeci.cloud_runner.client import CoreClient
from outcomeci.runtime import container as run_container
from outcomeci.runtime import launcher as workflow_run
from outcomeci.vault import local as local_vault
from outcomeci.vault.leases import LeaseResolver

LATER = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()


def _typed(refresh: str) -> dict:
    return {
        "credential_type": "oauth2",
        "configuration": {"client_id": "cid", "grant_type": "refresh_token"},
        "secrets": {"client_secret": "cs", "refresh_token": refresh},
    }


def test_a_lease_saves_a_rotation_before_it_keeps_it() -> None:
    saved: list[tuple[str, dict]] = []
    resolver = LeaseResolver(
        {"slack/app": _typed("rt-1")},
        LATER,
        on_rotate=lambda path, secrets: saved.append((path, secrets)),
    )
    resolver.rotate("vault:slack/app", {"refresh_token": "rt-2"})
    assert saved == [("slack/app", {"refresh_token": "rt-2"})]
    assert resolver("vault:slack/app")["secrets"]["refresh_token"] == "rt-2"
    assert resolver("vault:slack/app")["secrets"]["client_secret"] == "cs"


def test_a_lease_that_cannot_save_offers_no_rotation() -> None:
    assert LeaseResolver({}, LATER).rotate is None


def test_a_failed_save_leaves_the_lease_unchanged() -> None:
    def refuse(_path, _secrets):
        raise CloudRequestError("stale", 409)

    resolver = LeaseResolver({"slack/app": _typed("rt-1")}, LATER, on_rotate=refuse)
    with pytest.raises(CloudRequestError):
        resolver.rotate("vault:slack/app", {"refresh_token": "rt-2"})
    assert resolver("vault:slack/app")["secrets"]["refresh_token"] == "rt-1"


def test_the_container_records_rotations_in_the_output_mount(tmp_path: Path) -> None:
    resolver = run_container._lease_resolver({"slack/app": _typed("rt-1")}, LATER, tmp_path)
    resolver.rotate("vault:slack/app", {"refresh_token": "rt-2"})
    resolver.rotate("vault:slack/app", {"refresh_token": "rt-3"})
    recorded = json.loads((tmp_path / run_container.ROTATIONS).read_text())
    assert recorded == {"slack/app": {"refresh_token": "rt-3"}}
    assert (tmp_path / run_container.ROTATIONS).stat().st_mode & 0o777 == 0o600


def test_a_local_run_saves_rotations_to_the_local_vault(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    root, output = tmp_path / "repo", tmp_path / "out"
    root.mkdir()
    output.mkdir()
    local_vault.initialize(root)
    local_vault.put(root, "slack/app", json.dumps(_typed("rt-1")))
    (output / run_container.ROTATIONS).write_text(
        json.dumps({"slack/app": {"refresh_token": "rt-2"}})
    )
    workflow_run._save_local_rotations(root, output)
    assert local_vault.resolve(root, "vault:slack/app")["secrets"] == {
        "client_secret": "cs",
        "refresh_token": "rt-2",
    }


def test_a_cloud_run_saves_rotations_with_the_leased_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    sent: list[tuple] = []
    monkeypatch.setattr(
        workflow_run,
        "rotate_debug_vault_credential",
        lambda *args: sent.append(args) or 8,
    )
    (tmp_path / run_container.ROTATIONS).write_text(
        json.dumps({"slack/app": {"refresh_token": "rt-2"}})
    )
    lease = {"lease_id": "lease-1", "versions": {"slack/app": 7}}
    workflow_run._save_cloud_rotations("w1", "wf1", lease, tmp_path)
    assert sent == [("w1", "wf1", "lease-1", "slack/app", 7, {"refresh_token": "rt-2"})]
    assert not workflow_run._pending_rotations_dir().exists()


def test_an_unsent_cloud_rotation_is_kept_and_sent_next_time(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(workflow_run, "credentials_path", lambda: tmp_path / "config" / "c.json")

    def unavailable(*_args):
        raise CloudRequestError("offline", None)

    monkeypatch.setattr(workflow_run, "rotate_debug_vault_credential", unavailable)
    (tmp_path / run_container.ROTATIONS).write_text(
        json.dumps({"slack/app": {"refresh_token": "rt-2"}})
    )
    workflow_run._save_cloud_rotations(
        "w1", "wf1", {"lease_id": "lease-1", "versions": {"slack/app": 7}}, tmp_path
    )
    [pending] = list(workflow_run._pending_rotations_dir().glob("*.json"))
    assert pending.stat().st_mode & 0o777 == 0o600
    assert "rt-2" not in capsys.readouterr().err

    sent: list[tuple] = []
    monkeypatch.setattr(
        workflow_run, "rotate_debug_vault_credential", lambda *args: sent.append(args) or 8
    )
    workflow_run._flush_pending_rotations()
    assert sent == [("w1", "wf1", "lease-1", "slack/app", 7, {"refresh_token": "rt-2"})]
    assert not pending.exists()


def test_the_managed_runner_client_posts_the_rotation() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            url=str(request.url),
            body=json.loads(request.content),
            auth=request.headers["Authorization"],
        )
        return httpx.Response(200, json={"path": "slack/app", "version": 8})

    client = CoreClient(
        "https://api.example.test",
        "invocation-1",
        "bootstrap",
        "workflow",
        transport=httpx.MockTransport(handler),
    )
    version = client.workflow_vault_rotate(
        "lease-token", "vault-lease-1", "slack/app", 7, {"refresh_token": "rt-2"}
    )
    assert version == 8
    assert seen["url"] == (
        "https://api.example.test/v1/internal/workflow-invocations/invocation-1"
        "/vault-leases/vault-lease-1/rotate"
    )
    assert seen["body"] == {
        "lease_token": "lease-token",
        "path": "slack/app",
        "expected_version": 7,
        "secrets": {"refresh_token": "rt-2"},
    }


def test_the_debug_lease_rotation_endpoint(monkeypatch) -> None:
    from outcomeci import cloud

    seen: dict = {}

    def request(path, *, method="GET", body=None):
        seen.update(path=path, method=method, body=body)
        return 200, {"path": "slack/app", "version": 8}

    monkeypatch.setattr(cloud, "_authorized_request", request)
    assert (
        cloud.rotate_debug_vault_credential(
            "w1", "wf1", "l1", "slack/app", 7, {"refresh_token": "x"}
        )
        == 8
    )
    assert seen == {
        "path": "/workspaces/w1/workflows/wf1/debug-lease/vault/l1/rotate",
        "method": "POST",
        "body": {"path": "slack/app", "expected_version": 7, "secrets": {"refresh_token": "x"}},
    }
