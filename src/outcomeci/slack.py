"""Local, user-owned Slack app setup through the official Slack CLI."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Callable, Sequence

import yaml


class SlackError(RuntimeError):
    pass


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

PROJECT_RELATIVE = Path(".outcomeci/integrations/slack")
CONNECTION = {"ref": "slack_local", "provider": "slack", "delivery": "on_demand"}


def _manifest(name: str) -> dict[str, object]:
    return {
        "display_information": {
            "name": name,
            "description": "Bring human context into local OutcomeCI workflows.",
            "background_color": "#17131f",
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {"display_name": name, "always_online": False},
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "channels:history",
                    "channels:read",
                    "chat:write",
                    "groups:history",
                    "groups:read",
                    "im:history",
                    "im:read",
                    "im:write",
                    "usergroups:read",
                    "users:read",
                    "users:read.email",
                ]
            }
        },
        "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
    }


def _hooks() -> dict[str, object]:
    return {
        "hooks": {
            "get-manifest": "oci integration slack manifest --project .",
        },
        "config": {
            "sdk-managed-connection-enabled": True,
            "watch": {
                "manifest": {"paths": ["manifest.json"]},
            },
        },
    }


def scaffold(workspace: Path, name: str, *, force: bool = False) -> Path:
    project = workspace / PROJECT_RELATIVE
    project.mkdir(parents=True, exist_ok=True)
    slack_dir = project / ".slack"
    slack_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = project / "manifest.json"
    if force or not manifest_path.exists():
        manifest_path.write_text(json.dumps(_manifest(name), indent=2) + "\n", encoding="utf-8")
    managed_files = {
        slack_dir / "hooks.json": json.dumps(_hooks(), indent=2) + "\n",
        slack_dir / ".gitignore": "apps.dev.json\ncache/\noutcomeci-runtime.json\n",
    }
    for path, content in managed_files.items():
        path.write_text(content, encoding="utf-8")
    if force:
        for legacy in (project / "app.py", project / "pyproject.toml", project / "requirements.txt"):
            if legacy.exists():
                legacy.unlink()
    config_path = slack_dir / "config.json"
    if not config_path.exists():
        config_path.write_text(
            json.dumps({"manifest": {"source": "local"}, "project_id": str(uuid.uuid4())}, indent=2) + "\n",
            encoding="utf-8",
        )
    return project


def register_connection(workflow_path: Path) -> bool:
    try:
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        spec = document["spec"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise SlackError(f"could not read workflow {workflow_path}: {exc}") from exc
    connections = spec.get("connections", [])
    if not isinstance(connections, list):
        raise SlackError("spec.connections must be a list")
    existing = next((item for item in connections if isinstance(item, dict) and item.get("ref") == CONNECTION["ref"]), None)
    if existing is not None:
        if existing.get("provider") == "slack" and existing.get("delivery") == "on_demand":
            return False
        text = workflow_path.read_text(encoding="utf-8")
        pattern = r"(?ms)(-\s+ref:\s*slack_local\b.*?\n\s+delivery:\s*)[^\s#]+"
        updated, count = re.subn(pattern, r"\1on_demand", text, count=1)
        if count != 1:
            raise SlackError("could not migrate the existing slack_local connection")
        workflow_path.write_text(updated, encoding="utf-8")
        return True

    text = workflow_path.read_text(encoding="utf-8")
    empty = re.search(r"(?m)^(?P<indent>\s{2})connections:\s*\[\]\s*$", text)
    block = (
        "  connections:\n"
        "    - ref: slack_local\n"
        "      provider: slack\n"
        "      delivery: on_demand"
    )
    if empty:
        updated = text[: empty.start()] + block + text[empty.end() :]
    else:
        start = re.search(r"(?m)^  connections:\s*$", text)
        if start:
            following = re.search(r"(?m)^(?:[^ \n]|  [A-Za-z_][A-Za-z0-9_-]*:)\s*", text[start.end() :])
            insert_at = start.end() + (following.start() if following else len(text[start.end() :]))
            entry = "\n    - ref: slack_local\n      provider: slack\n      delivery: on_demand"
            updated = text[:insert_at].rstrip("\n") + entry + "\n" + text[insert_at:].lstrip("\n")
        else:
            updated = text.rstrip() + "\n" + block + "\n"
    workflow_path.write_text(updated, encoding="utf-8")
    return True


def _invoke(
    command: Sequence[str],
    *,
    cwd: Path,
    runner: CommandRunner,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        cwd=cwd,
        text=True,
        check=False,
        capture_output=capture,
    )


def _require_slack() -> str:
    executable = shutil.which("slack")
    if executable is None:
        raise SlackError(
            "Slack CLI is not installed; install it from "
            "https://docs.slack.dev/tools/slack-cli/guides/installing-the-slack-cli-for-mac-and-linux/"
        )
    return executable


def _authorized(slack: str, project: Path, runner: CommandRunner) -> bool:
    result = _invoke([slack, "auth", "list", "--no-color"], cwd=project, runner=runner, capture=True)
    return result.returncode == 0 and bool(re.search(r"Team ID:\s*[A-Z0-9]+", result.stdout or ""))


def _expired(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "auth_token_error" in output or "token_expired" in output or "access token has expired" in output


def _validate(slack: str, project: Path, runner: CommandRunner, *, app_id: str | None = None, team: str | None = None) -> subprocess.CompletedProcess[str]:
    command = [slack, "manifest", "validate", "--no-color"]
    if app_id:
        command.extend(["--app", app_id])
    elif team:
        command.extend(["--team", team])
    return _invoke(command, cwd=project, runner=runner, capture=True)


def setup(
    workspace: Path,
    *,
    name: str = "OutcomeCI",
    team: str | None = None,
    channel: str | None = None,
    force: bool = False,
    runner: CommandRunner = subprocess.run,
) -> dict[str, object]:
    workspace = workspace.resolve()
    workflow = workspace / "outcome.yml"
    if not workflow.is_file():
        raise SlackError(f"{workflow} does not exist; run `oci init --backend filesystem` first")
    slack = _require_slack()
    project = scaffold(workspace, name, force=force)
    if channel:
        (project / "config.json").write_text(
            json.dumps({"default_target": channel.strip()}, indent=2) + "\n",
            encoding="utf-8",
        )

    if not _authorized(slack, project, runner):
        login = _invoke([slack, "login"], cwd=project, runner=runner)
        if login.returncode != 0:
            raise SlackError("Slack CLI login did not complete")
        if not _authorized(slack, project, runner):
            raise SlackError("Slack CLI has no authorized workspace after login")

    app_id = installed_app_id(project, team)
    validate = _validate(slack, project, runner, app_id=app_id, team=team)
    if _expired(validate):
        login = _invoke([slack, "login"], cwd=project, runner=runner)
        if login.returncode != 0:
            raise SlackError("Slack CLI login did not complete")
        validate = _validate(slack, project, runner, app_id=app_id, team=team)
    if validate.returncode != 0 or _expired(validate):
        detail = (validate.stderr or validate.stdout or "unknown Slack CLI error").strip()
        raise SlackError(f"Slack rejected the generated app manifest: {detail}")
    command = [slack, "app", "install"]
    if app_id:
        command.extend(["--app", app_id])
    else:
        command.extend(["--environment", "local"])
        if team:
            command.extend(["--team", team])
    install = _invoke(command, cwd=project, runner=runner)
    if install.returncode != 0:
        raise SlackError("Slack app installation did not complete")
    changed = register_connection(workflow)
    return {
        "configured": True,
        "connection": CONNECTION,
        "project": str(project),
        "workflow_updated": changed,
        "next": "oci integration slack targets --workspace " + str(workspace),
    }


def api(workspace: Path, method: str, payload: dict[str, object] | None = None, *, runner: CommandRunner = subprocess.run) -> dict[str, object]:
    """Call Slack through its CLI; authentication and opaque IDs stay internal."""
    project = workspace.resolve() / PROJECT_RELATIVE
    slack = _require_slack()
    app_id = installed_app_id(project)
    if not app_id:
        raise SlackError("Slack app installation is ambiguous; select one with `oci integration slack setup --team <team>`")
    command = [slack, "api", method, "--app", app_id]
    for key, value in (payload or {}).items():
        rendered = json.dumps(value, separators=(",", ":")) if not isinstance(value, str) else value
        command.append(f"{key}={rendered}")
    result = _invoke(command, cwd=project, runner=runner, capture=True)
    if result.returncode != 0:
        raise SlackError(f"Slack API call failed for {method}: {(result.stderr or result.stdout or 'unknown error').strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SlackError(f"Slack returned an invalid response for {method}") from exc
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise SlackError(f"Slack API call failed for {method}: {value.get('error', 'unknown error') if isinstance(value, dict) else 'invalid response'}")
    return value


def targets(workspace: Path, *, runner: CommandRunner = subprocess.run) -> dict[str, list[str]]:
    """Return readable selectors only; never expose provider IDs."""
    users_value = api(workspace, "users.list", runner=runner)
    channels_value = api(workspace, "conversations.list", {"types": "public_channel,private_channel", "limit": 999}, runner=runner)
    groups_value = api(workspace, "usergroups.list", {"include_users": True}, runner=runner)
    users = []
    for item in users_value.get("members", []):
        if not isinstance(item, dict) or item.get("deleted") or item.get("is_bot") or item.get("id") == "USLACKBOT" or str(item.get("name", "")).casefold() == "slackbot":
            continue
        profile = item.get("profile", {}) if isinstance(item.get("profile"), dict) else {}
        name = profile.get("display_name") or item.get("name") or profile.get("real_name")
        if isinstance(name, str) and name:
            users.append(name)
    channels = [item["name"] for item in channels_value.get("channels", []) if isinstance(item, dict) and isinstance(item.get("name"), str)]
    groups = [item.get("handle") or item.get("name") for item in groups_value.get("usergroups", []) if isinstance(item, dict) and isinstance(item.get("handle") or item.get("name"), str)]
    return {"users": sorted(set(users), key=str.casefold), "channels": sorted(set(channels), key=str.casefold), "groups": sorted(set(groups), key=str.casefold)}


def _directory(workspace: Path, *, runner: CommandRunner = subprocess.run) -> dict[str, dict[str, object]]:
    users_value = api(workspace, "users.list", runner=runner)
    channels_value = api(workspace, "conversations.list", {"types": "public_channel,private_channel", "limit": 999}, runner=runner)
    groups_value = api(workspace, "usergroups.list", {"include_users": True}, runner=runner)
    users: dict[str, object] = {}
    user_names: dict[str, str] = {}
    for item in users_value.get("members", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item.get("deleted") or item.get("is_bot") or item.get("id") == "USLACKBOT" or str(item.get("name", "")).casefold() == "slackbot":
            continue
        profile = item.get("profile", {}) if isinstance(item.get("profile"), dict) else {}
        names = {item.get("name"), profile.get("display_name"), profile.get("real_name")}
        readable = next((name for name in (profile.get("display_name"), item.get("name"), profile.get("real_name")) if isinstance(name, str) and name), item["id"])
        user_names[item["id"]] = str(readable)
        for name in names:
            if isinstance(name, str) and name:
                users[name.casefold()] = item["id"]
    channels = {
        str(item["name"]).casefold(): item["id"] for item in channels_value.get("channels", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("id"), str)
    }
    groups = {
        str(item.get("handle") or item.get("name")).casefold(): item for item in groups_value.get("usergroups", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("handle") or item.get("name"), str)
    }
    return {"users": users, "user_names": user_names, "channels": channels, "groups": groups}


def _runtime_path(workspace: Path) -> Path:
    return workspace / PROJECT_RELATIVE / ".slack" / "outcomeci-runtime.json"


def _runtime(workspace: Path) -> dict[str, object]:
    try:
        value = json.loads(_runtime_path(workspace).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_runtime(workspace: Path, value: dict[str, object]) -> None:
    path = _runtime_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def deliver(workspace: Path, request: dict[str, object], *, runner: CommandRunner = subprocess.run) -> dict[str, object]:
    """Resolve readable targets privately and persist private thread receipts."""
    delivery = request.get("delivery", {})
    selectors = delivery.get("targets", []) if isinstance(delivery, dict) else []
    if not selectors:
        raise SlackError("the human hook has no Slack targets")
    key = f"{request.get('run_id')}:{request.get('phase')}:{request.get('id')}"
    runtime = _runtime(workspace)
    interactions = runtime.setdefault("interactions", {})
    existing = interactions.get(key) if isinstance(interactions, dict) else None
    if isinstance(existing, dict) and isinstance(existing.get("receipts"), list):
        return {"delivered": True, "targets": selectors, "threads": len(existing["receipts"]), "existing": True}
    directory = _directory(workspace, runner=runner)
    destination_ids: set[str] = set()
    for selector in selectors:
        if not isinstance(selector, dict):
            continue
        kind = selector.get("kind")
        name = str(selector.get("name", "")).lstrip("@#").casefold()
        found = False
        if kind == "channel":
            identifier = directory["channels"].get(name)
            if isinstance(identifier, str):
                destination_ids.add(identifier)
                found = True
        elif kind == "user":
            identifier = directory["users"].get(name)
            if isinstance(identifier, str):
                opened = api(workspace, "conversations.open", {"users": identifier}, runner=runner)
                channel = opened.get("channel", {})
                if isinstance(channel, dict) and isinstance(channel.get("id"), str):
                    destination_ids.add(channel["id"])
                    found = True
        elif kind == "group":
            group = directory["groups"].get(name)
            if isinstance(group, dict):
                members = group.get("users", [])
                if not members:
                    members = api(workspace, "usergroups.users.list", {"usergroup": group["id"]}, runner=runner).get("users", [])
                for identifier in members:
                    if isinstance(identifier, str):
                        opened = api(workspace, "conversations.open", {"users": identifier}, runner=runner)
                        channel = opened.get("channel", {})
                        if isinstance(channel, dict) and isinstance(channel.get("id"), str):
                            destination_ids.add(channel["id"])
                            found = True
        if not found:
            raise SlackError(f"Slack target {selector.get('kind')}:{selector.get('name')} was not found")
    text = f"*OutcomeCI needs {str(request.get('interaction', 'input')).replace('_', ' ')}*\n{request.get('purpose', 'Input is required to continue this outcome.')}\nReply in this thread."
    receipts = []
    for channel in sorted(destination_ids):
        response = api(workspace, "chat.postMessage", {"channel": channel, "text": text}, runner=runner)
        receipts.append({"channel": response.get("channel", channel), "thread_ts": response.get("ts")})
    if isinstance(interactions, dict):
        interactions[key] = {"receipts": receipts}
    _write_runtime(workspace, runtime)
    return {"delivered": True, "targets": selectors, "threads": len(receipts)}


def poll_replies(workspace: Path, run_id: str, interaction_id: str, *, runner: CommandRunner = subprocess.run) -> list[dict[str, str]]:
    """Read replies while returning only human-readable identity data."""
    runtime = _runtime(workspace)
    interactions = runtime.get("interactions", {})
    matches = [value for key, value in interactions.items() if isinstance(value, dict) and key.startswith(f"{run_id}:") and key.endswith(f":{interaction_id}")] if isinstance(interactions, dict) else []
    if len(matches) != 1:
        raise SlackError(f"no delivered Slack interaction named {interaction_id} exists for this outcome")
    directory = _directory(workspace, runner=runner)
    replies: list[dict[str, str]] = []
    for receipt in matches[0].get("receipts", []):
        if not isinstance(receipt, dict):
            continue
        value = api(workspace, "conversations.replies", {"channel": receipt.get("channel"), "ts": receipt.get("thread_ts")}, runner=runner)
        for message in value.get("messages", [])[1:]:
            if not isinstance(message, dict) or message.get("bot_id"):
                continue
            user = directory["user_names"].get(message.get("user"), "Slack user")
            replies.append({"from": str(user), "message": str(message.get("text", "")), "responded_at": str(message.get("ts", ""))})
    return replies


def status(workspace: Path, *, runner: CommandRunner = subprocess.run) -> dict[str, object]:
    workspace = workspace.resolve()
    project = workspace / PROJECT_RELATIVE
    workflow = workspace / "outcome.yml"
    executable = shutil.which("slack")
    connection = False
    if workflow.is_file():
        try:
            document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
            items = document.get("spec", {}).get("connections", [])
            connection = any(isinstance(item, dict) and item.get("ref") == "slack_local" for item in items)
        except (OSError, yaml.YAMLError, AttributeError):
            pass
    authorized = False
    if executable and project.is_dir() and _authorized(executable, project, runner):
        authorized = not _expired(_validate(executable, project, runner, app_id=installed_app_id(project)))
    return {
        "ready": bool(executable and project.is_dir() and connection and authorized),
        "cli_installed": executable is not None,
        "authorized": authorized,
        "project_created": (project / ".slack/hooks.json").is_file(),
        "workflow_registered": connection,
        "project": str(project),
    }


def installed_app_id(project: Path, team: str | None = None) -> str | None:
    """Return one unambiguous installed app, otherwise preserve Slack's selector."""
    path = project / ".slack" / "apps.dev.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    installations = [item for item in value.values() if isinstance(item, dict)]
    if team:
        installations = [
            item for item in installations
            if item.get("team_id") == team or item.get("team_domain") == team
        ]
    app_ids = {item.get("app_id") for item in installations if isinstance(item.get("app_id"), str)}
    return next(iter(app_ids)) if len(app_ids) == 1 else None


def run(workspace: Path, *, team: str | None = None, runner: CommandRunner = subprocess.run) -> int:
    workspace = workspace.resolve()
    project = workspace / PROJECT_RELATIVE
    if not (project / "manifest.json").is_file() or not (project / ".slack/hooks.json").is_file():
        raise SlackError("Slack integration is not configured; run `oci integration slack setup` first")
    slack = _require_slack()
    command = [slack, "run"]
    app_id = installed_app_id(project, team)
    if app_id:
        command.extend(["--app", app_id])
    elif team:
        command.extend(["--team", team])
    return _invoke(command, cwd=project, runner=runner).returncode


def manifest(project: Path) -> dict[str, object]:
    path = project.resolve() / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SlackError(f"could not read Slack manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SlackError("Slack manifest must be a JSON object")
    return value
