"""Copy an installed Slack app credential into an OutcomeCI Vault."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from outcomeci_connectors.providers.slack.setup import PROJECT_RELATIVE, SlackError, _require_slack

from .cloud import vault_request
from .local_vault import VAULT_FILE, initialize, is_valid_vault_path, put


def _installation(project: Path, team: str | None) -> dict[str, Any]:
    try:
        values = json.loads((project / ".slack/apps.dev.json").read_text())
        installations = [v for v in values.values() if isinstance(v, dict)]
    except (OSError, ValueError, AttributeError):
        raise SlackError(
            "Slack app is not installed; run `oci integration slack setup` first"
        ) from None
    if team:
        installations = [
            v for v in installations if team in (v.get("team_id"), v.get("team_domain"))
        ]
    if (
        len(installations) != 1
        or not installations[0].get("app_id")
        or not installations[0].get("team_id")
    ):
        raise SlackError("Select one installed Slack workspace with --team")
    return installations[0]


def _call(client: httpx.Client, method: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.post(
            "https://slack.com/api/" + method,
            headers={"Authorization": "Bearer " + token},
            json=body,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError()
        return result
    except (httpx.HTTPError, ValueError):
        # Never forward provider bodies, subprocess output, or authorization headers.
        raise SlackError(
            f"Slack {method} failed; check app access and run `slack login` if needed"
        ) from None


def sync_credentials(
    workspace: Path,
    *,
    local: bool = False,
    cloud_workspace: str | None = None,
    vault_workspace: Path | None = None,
    team: str | None = None,
    path: str = "slack/bot-token",
    workflows: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if local == bool(cloud_workspace):
        raise SlackError("Select exactly one destination: --local or --cloud WORKSPACE_ID")
    if not is_valid_vault_path(path):
        raise SlackError("Vault path must be a relative logical path")
    if not local and vault_workspace is not None:
        raise SlackError("--vault-workspace is only valid with --local")
    if local and workflows:
        raise SlackError("--workflow grants are only valid with --cloud")
    project = workspace.resolve() / PROJECT_RELATIVE
    installation = _installation(project, team)
    existing = None
    if cloud_workspace:
        inventory = vault_request(cloud_workspace, "list")
        if not isinstance(inventory, dict) or not isinstance(inventory.get("entries"), list):
            raise SlackError("Cloud Vault returned an invalid inventory")
        existing = next((v for v in inventory["entries"] if v["path"] == path), None)
        if existing and (
            existing.get("kind") != "secret"
            or existing.get("provider")
            or existing.get("status") != "active"
        ):
            raise SlackError(
                "Destination path is not an active generic secret; select another --path"
            )
    # Let Slack CLI refresh its own tooling authorization before reading it.
    try:
        refreshed = subprocess.run(
            [_require_slack(), "auth", "list", "--skip-update", "--no-color"],
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if refreshed.returncode:
            raise ValueError()
        credentials = json.loads((Path.home() / ".slack/credentials.json").read_text())
        auth = credentials[installation["team_id"]]
        token = auth["token"]
        if auth.get("exp") and auth["exp"] <= time.time():
            raise ValueError()
        if not isinstance(token, str) or not token:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise SlackError("Slack CLI authorization is unavailable; run `slack login`") from None
    with httpx.Client(transport=transport, timeout=30, follow_redirects=False) as client:
        # Use installed scopes, not edited local scopes: syncing must not expand app access.
        manifest = _call(
            client, "apps.manifest.export", token, {"app_id": installation["app_id"]}
        ).get("manifest", {})
        try:
            scopes = manifest["oauth_config"]["scopes"]["bot"]
            if (
                not isinstance(scopes, list)
                or not scopes
                or not all(isinstance(s, str) for s in scopes)
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SlackError(
                "Installed Slack app has no valid bot scopes; rerun Slack setup"
            ) from None
        result = _call(
            client,
            "apps.developerInstall",
            token,
            {
                "app_id": installation["app_id"],
                "bot_scopes": scopes,
                "outgoing_domains": manifest.get("outgoing_domains", []),
            },
        )
        if result.get("app_id") != installation["app_id"]:
            raise SlackError("Slack returned a credential for a different app")
        tokens = result.get("api_access_tokens")
        bot = tokens.get("bot") if isinstance(tokens, dict) else None
        if not isinstance(bot, str) or re.fullmatch(r"xoxb-[A-Za-z0-9-]+", bot) is None:
            raise SlackError("Installed Slack app did not return a bot credential")
        identity = _call(client, "auth.test", bot, {})
        if identity.get("team_id") != installation["team_id"] or not identity.get("bot_id"):
            raise SlackError("Slack credential does not match the selected bot installation")
    if local:
        destination = (vault_workspace or workspace).resolve()
        if not (destination / VAULT_FILE).exists():
            initialize(destination)
        put(destination, path, bot)
        return {"synced": True, "destination": "local", "workspace": str(destination), "path": path}
    try:
        if existing:
            vault_request(cloud_workspace, "rotate", entry_id=existing["id"], value=bot)
            if workflows is not None:
                vault_request(
                    cloud_workspace, "grant", entry_id=existing["id"], workflow_ids=workflows
                )
        else:
            vault_request(
                cloud_workspace,
                "put",
                path=path,
                display_name="Slack bot token",
                value=bot,
                workflow_ids=workflows or [],
            )
    except Exception:
        raise SlackError(
            "Cloud Vault sync failed; check Vault permissions and workflow grants"
        ) from None
    return {"synced": True, "destination": "cloud", "workspace": cloud_workspace, "path": path}
