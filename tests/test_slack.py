from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from outcomeci import slack as slack_module
from outcomeci.cli import main
from outcomeci.repository import initialize
from outcomeci.slack import (
    SlackError,
    installed_app_id,
    manifest,
    register_connection,
    run,
    scaffold,
    setup,
    status,
)


class FakeSlack:
    def __init__(self, *, initially_authorized: bool = True, expired: bool = False) -> None:
        self.authorized = initially_authorized
        self.expired = expired
        self.commands: list[tuple[list[str], Path]] = []

    def __call__(self, command, *, cwd, text, check, capture_output=False):
        self.commands.append((list(command), Path(cwd)))
        if command[1:3] == ["auth", "list"]:
            stdout = "local (Team ID: T0123456)\n" if self.authorized else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        if command[1:] == ["login"]:
            self.authorized = True
            self.expired = False
        if command[1:3] == ["manifest", "validate"] and self.expired:
            return subprocess.CompletedProcess(
                command, 0, stdout="auth_token_error: token_expired", stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_scaffold_writes_on_demand_slack_project(tmp_path: Path) -> None:
    project = scaffold(tmp_path, "Acme Outcomes")
    value = manifest(project)
    assert value["display_information"]["name"] == "Acme Outcomes"
    assert value["settings"]["socket_mode_enabled"] is False
    assert value["features"]["app_home"]["messages_tab_enabled"] is True
    assert "slash_commands" not in value["features"]
    assert "commands" not in value["oauth_config"]["scopes"]["bot"]
    assert "event_subscriptions" not in value["settings"]
    assert not (project / "app.py").exists()
    hooks = json.loads((project / ".slack/hooks.json").read_text())
    assert hooks["hooks"]["get-manifest"] == "oci integration slack manifest --project ."
    assert "start" not in hooks["hooks"]


def test_scaffold_preserves_existing_manifest_without_force(tmp_path: Path) -> None:
    project = scaffold(tmp_path, "First")
    scaffold(tmp_path, "Second")
    assert manifest(project)["display_information"]["name"] == "First"
    scaffold(tmp_path, "Second", force=True)
    assert manifest(project)["display_information"]["name"] == "Second"


def test_register_connection_is_idempotent_and_preserves_workflow_text(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
    original = workflow.read_text()
    assert register_connection(workflow) is True
    updated = workflow.read_text()
    assert updated != original
    assert register_connection(workflow) is False
    assert workflow.read_text() == updated
    connection = yaml.safe_load(updated)["spec"]["connections"][0]
    assert connection == {"ref": "slack_local", "provider": "slack", "delivery": "on_demand"}


def test_register_connection_migrates_socket_mode_in_place(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
    register_connection(workflow)
    workflow.write_text(
        workflow.read_text().replace("delivery: on_demand", "delivery: socket_mode")
    )
    assert register_connection(workflow) is True
    assert "delivery: on_demand" in workflow.read_text()


def test_setup_logs_in_when_needed_and_installs_app(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    fake = FakeSlack(initially_authorized=False)
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/local/bin/slack")
    result = setup(tmp_path, name="Acme Outcomes", team="T0123456", channel="C0123456", runner=fake)
    commands = [command for command, _ in fake.commands]
    assert ["/usr/local/bin/slack", "login"] in commands
    assert [
        "/usr/local/bin/slack",
        "manifest",
        "validate",
        "--no-color",
        "--team",
        "T0123456",
    ] in commands
    assert [
        "/usr/local/bin/slack",
        "app",
        "install",
        "--environment",
        "local",
        "--team",
        "T0123456",
    ] in commands
    assert result["configured"] is True
    assert result["workflow_updated"] is True
    assert (
        json.loads((tmp_path / ".outcomeci/integrations/slack/config.json").read_text())[
            "default_target"
        ]
        == "C0123456"
    )


def test_setup_skips_login_for_authorized_workspace(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    fake = FakeSlack()
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    setup(tmp_path, runner=fake)
    assert ["/usr/bin/slack", "login"] not in [command for command, _ in fake.commands]


def test_setup_targets_unambiguous_existing_app(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    project = scaffold(tmp_path, "Acme")
    apps = project / ".slack/apps.dev.json"
    apps.parent.mkdir(parents=True, exist_ok=True)
    apps.write_text('{"T1":{"app_id":"A1","team_id":"T1"}}')
    fake = FakeSlack()
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    setup(tmp_path, name="Acme", force=True, runner=fake)
    commands = [command for command, _ in fake.commands]
    assert ["/usr/bin/slack", "manifest", "validate", "--no-color", "--app", "A1"] in commands
    assert ["/usr/bin/slack", "app", "install", "--app", "A1"] in commands


def test_setup_reauthenticates_an_expired_slack_session(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    fake = FakeSlack(expired=True)
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    setup(tmp_path, runner=fake)
    assert ["/usr/bin/slack", "login"] in [command for command, _ in fake.commands]


def test_status_reports_ready_configuration(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    scaffold(tmp_path, "OutcomeCI")
    register_connection(tmp_path / "outcome.yml")
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    assert status(tmp_path, runner=FakeSlack())["ready"] is True


def test_run_delegates_to_slack_cli(tmp_path: Path, monkeypatch) -> None:
    scaffold(tmp_path, "OutcomeCI")
    fake = FakeSlack()
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    assert run(tmp_path, team="T0123456", runner=fake) == 0
    assert fake.commands[-1][0] == ["/usr/bin/slack", "run", "--team", "T0123456"]


def test_run_uses_the_only_installed_app_without_prompt(tmp_path: Path, monkeypatch) -> None:
    project = scaffold(tmp_path, "OutcomeCI")
    (project / ".slack/apps.dev.json").write_text(
        json.dumps(
            {"T0123456": {"app_id": "A0123456789", "team_id": "T0123456", "team_domain": "acme"}}
        )
    )
    fake = FakeSlack()
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    assert installed_app_id(project) == "A0123456789"
    assert run(tmp_path, runner=fake) == 0
    assert fake.commands[-1][0] == ["/usr/bin/slack", "run", "--app", "A0123456789"]


def test_run_preserves_selector_when_installation_is_ambiguous(tmp_path: Path, monkeypatch) -> None:
    project = scaffold(tmp_path, "OutcomeCI")
    (project / ".slack/apps.dev.json").write_text(
        json.dumps(
            {
                "T1": {"app_id": "A1", "team_id": "T1"},
                "T2": {"app_id": "A2", "team_id": "T2"},
            }
        )
    )
    fake = FakeSlack()
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: "/usr/bin/slack")
    assert installed_app_id(project) is None
    assert run(tmp_path, runner=fake) == 0
    assert fake.commands[-1][0] == ["/usr/bin/slack", "run"]


def test_setup_requires_slack_cli(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    monkeypatch.setattr("outcomeci.slack.shutil.which", lambda _: None)
    with pytest.raises(SlackError, match="Slack CLI is not installed"):
        setup(tmp_path)


def test_cli_manifest_hook_emits_json(tmp_path: Path, capsys) -> None:
    project = scaffold(tmp_path, "OutcomeCI")
    assert main(["integration", "slack", "manifest", "--project", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["display_information"]["name"] == "OutcomeCI"
    assert main(["integration", "slack", "manifest", f"--source={project}"]) == 0


def test_targets_expose_names_without_slack_ids(tmp_path: Path, monkeypatch) -> None:
    responses = {
        "users.list": {
            "ok": True,
            "members": [{"id": "U123", "name": "isaah", "profile": {"display_name": "Isaah"}}],
        },
        "conversations.list": {"ok": True, "channels": [{"id": "C123", "name": "product"}]},
        "usergroups.list": {"ok": True, "usergroups": [{"id": "S123", "handle": "design"}]},
    }
    monkeypatch.setattr(
        slack_module, "api", lambda workspace, method, payload=None, runner=None: responses[method]
    )
    result = slack_module.targets(tmp_path)
    assert result == {"users": ["Isaah"], "channels": ["product"], "groups": ["design"]}
    assert not any("123" in value for values in result.values() for value in values)
