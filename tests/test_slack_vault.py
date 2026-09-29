from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from outcomeci_connectors.providers.slack.setup import PROJECT_RELATIVE, SlackError

from outcomeci import slack_vault
from outcomeci.cli import main
from outcomeci.local_vault import list_entries, resolve

BOT = "xoxb-private-test-credential"
TOOLING = "xoxe-private-tooling-credential"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    workspace = tmp_path / "workflow"
    project = workspace / PROJECT_RELATIVE
    (project / ".slack").mkdir(parents=True)
    (project / ".slack/apps.dev.json").write_text(
        json.dumps({"one": {"app_id": "A1", "team_id": "T1", "team_domain": "acme"}})
    )
    home = tmp_path / "home"
    (home / ".slack").mkdir(parents=True)
    (home / ".slack/credentials.json").write_text(json.dumps({"T1": {"token": TOOLING}}))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(slack_vault, "_require_slack", lambda: "/bin/slack")
    monkeypatch.setattr(
        slack_vault.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    return workspace


def transport(*, mismatch=False, failure=False):
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if failure:
            return httpx.Response(403, json={"error": BOT + TOOLING})
        if method == "apps.manifest.export":
            assert request.headers["authorization"] == "Bearer " + TOOLING
            assert body == {"app_id": "A1"}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "manifest": {"oauth_config": {"scopes": {"bot": ["chat:write"]}}},
                },
            )
        if method == "apps.developerInstall":
            assert body["app_id"] == "A1"
            assert body["bot_scopes"] == ["chat:write"]
            return httpx.Response(
                200, json={"ok": True, "app_id": "A1", "api_access_tokens": {"bot": BOT}}
            )
        assert method == "auth.test"
        assert request.headers["authorization"] == "Bearer " + BOT
        return httpx.Response(
            200, json={"ok": True, "team_id": "wrong" if mismatch else "T1", "bot_id": "B1"}
        )

    return httpx.MockTransport(handler)


def test_local_sync_encrypted_and_rotatable(installed):
    destination = installed.parent / "other-workflow"
    for _ in range(2):
        result = slack_vault.sync_credentials(
            installed, local=True, vault_workspace=destination, transport=transport()
        )
        assert BOT not in json.dumps(result)
        assert resolve(destination, "vault:slack/bot-token") == BOT
    assert BOT not in (destination / ".outcomeci/vault.enc").read_text()
    assert len(list_entries(destination)["entries"]) == 1


@pytest.mark.parametrize("existing", [False, True])
def test_cloud_sync_preserves_grants_on_rotation(installed, monkeypatch, existing):
    calls = []

    def vault(workspace, operation, **kwargs):
        calls.append((workspace, operation, kwargs))
        if operation == "list":
            return {
                "entries": [
                    {
                        "id": "entry",
                        "path": "slack/bot-token",
                        "kind": "secret",
                        "status": "active",
                        "provider": None,
                        "workflow_ids": ["existing-workflow"],
                    }
                ]
                if existing
                else []
            }
        return {"id": "entry"}

    monkeypatch.setattr(slack_vault, "vault_request", vault)
    result = slack_vault.sync_credentials(
        installed, cloud_workspace="workspace_test", transport=transport()
    )
    assert [c[1] for c in calls] == ["list", "rotate" if existing else "put"]
    assert calls[-1][2]["value"] == BOT
    assert BOT not in json.dumps(result)


def test_explicit_cloud_grants(installed, monkeypatch):
    calls = []

    def vault(workspace, operation, **kwargs):
        calls.append((operation, kwargs))
        return (
            {
                "entries": [
                    {"id": "entry", "path": "slack/bot-token", "kind": "secret", "status": "active"}
                ]
            }
            if operation == "list"
            else {}
        )

    monkeypatch.setattr(slack_vault, "vault_request", vault)
    slack_vault.sync_credentials(
        installed, cloud_workspace="workspace_test", workflows=["workflow"], transport=transport()
    )
    assert calls[-1] == ("grant", {"entry_id": "entry", "workflow_ids": ["workflow"]})


@pytest.mark.parametrize("failure,mismatch", [(True, False), (False, True)])
def test_failed_or_wrong_identity_never_stored(installed, failure, mismatch):
    with pytest.raises(SlackError) as exc:
        slack_vault.sync_credentials(
            installed, local=True, transport=transport(failure=failure, mismatch=mismatch)
        )
    assert BOT not in str(exc.value) and TOOLING not in str(exc.value)
    assert not (installed / ".outcomeci/vault.enc").exists()


def test_ambiguous_install_requires_team(installed):
    p = installed / PROJECT_RELATIVE / ".slack/apps.dev.json"
    d = json.loads(p.read_text())
    d["two"] = {"app_id": "A1", "team_id": "T2", "team_domain": "other"}
    p.write_text(json.dumps(d))
    with pytest.raises(SlackError, match="--team"):
        slack_vault.sync_credentials(installed, local=True, transport=transport())
    slack_vault.sync_credentials(installed, local=True, team="acme", transport=transport())


def test_cli_routes_destination_without_token_output(installed, monkeypatch, capsys):
    def sync(workspace, **kwargs):
        assert workspace == installed
        assert kwargs["local"] and kwargs["path"] == "slack/bot-token"
        return {"synced": True}

    monkeypatch.setattr(slack_vault, "sync_credentials", sync)
    assert (
        main(["integration", "slack", "sync-credentials", "--dir", str(installed), "--local"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == {"synced": True}


def test_cloud_failure_does_not_echo_secret(installed, monkeypatch):
    def vault(workspace, operation, **kwargs):
        if operation == "list":
            return {"entries": []}
        raise RuntimeError(BOT)

    monkeypatch.setattr(slack_vault, "vault_request", vault)
    with pytest.raises(SlackError) as exc:
        slack_vault.sync_credentials(
            installed, cloud_workspace="workspace_test", transport=transport()
        )
    assert BOT not in str(exc.value)
