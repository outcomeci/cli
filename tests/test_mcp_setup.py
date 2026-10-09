import json
import subprocess

import pytest

from outcomeci import cli, mcp_setup


class Recorder:
    """Stands in for subprocess.run: records commands and answers `mcp get`."""

    def __init__(self, configured=(), fail=()):
        self.configured = set(configured)
        self.fail = set(fail)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[1:3] == ["mcp", "get"]:
            code = 0 if (command[0], command[3]) in self.configured else 1
        else:
            code = 1 if command[0] in self.fail else 0
        return subprocess.CompletedProcess(command, code)

    def ran(self, *prefix):
        return [command for command in self.commands if command[: len(prefix)] == list(prefix)]


def installed(*names):
    return lambda executable: f"/bin/{executable}" if executable in names else None


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OUTCOMECI_API_URL", raising=False)


def test_adds_the_server_to_every_installed_agent():
    run = Recorder()
    result = mcp_setup.init(
        api_url="https://api.outcomeci.com/",
        which=installed("claude", "codex", "opencode"),
        run=run,
    )
    url = "https://api.outcomeci.com/v1/mcp"
    assert result["url"] == url
    assert run.ran("/bin/claude", "mcp", "add") == [
        ["/bin/claude", "mcp", "add", "--transport", "http", "--scope", "user", "outcomeci", url]
    ]
    assert run.ran("/bin/codex", "mcp", "add") == [
        ["/bin/codex", "mcp", "add", "outcomeci", "--url", url]
    ]
    assert run.ran("/bin/opencode", "mcp", "add") == [
        ["/bin/opencode", "mcp", "add", "outcomeci", "--url", url]
    ]
    # OpenCode signs in with its own command; the others sign in elsewhere.
    assert run.ran("/bin/opencode", "mcp", "auth") == [
        ["/bin/opencode", "mcp", "auth", "outcomeci"]
    ]
    by_agent = {entry["agent"]: entry for entry in result["agents"]}
    assert by_agent["opencode"]["signed_in"] is True
    assert "/mcp" in by_agent["claude"]["next_step"]
    assert all(entry["status"] == "added" for entry in result["agents"])


def test_skips_agents_that_already_reach_outcomeci(tmp_path):
    config = tmp_path / "xdg" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(
        '{\n  // added earlier\n  "mcp": {"outcomeci": {"type": "remote", "url": "https://x/v1/mcp"},},\n}\n'
    )
    run = Recorder(configured={("/bin/claude", "claude.ai OutcomeCI"), ("/bin/codex", "outcomeci")})
    result = mcp_setup.init(
        api_url="https://api.outcomeci.com",
        which=installed("claude", "codex", "opencode"),
        run=run,
    )
    assert [entry["status"] for entry in result["agents"]] == ["already_configured"] * 3
    assert result["agents"][0]["name"] == "claude.ai OutcomeCI"
    assert not [command for command in run.commands if "add" in command]


def test_reports_missing_and_failed_agents_and_skips_sign_in_without_a_terminal():
    run = Recorder(fail={"/bin/codex"})
    result = mcp_setup.init(
        api_url="https://api.outcomeci.com",
        which=installed("codex", "opencode"),
        run=run,
        login=False,
    )
    statuses = {entry["agent"]: entry["status"] for entry in result["agents"]}
    assert statuses == {"claude": "not_installed", "codex": "failed", "opencode": "added"}
    assert not run.ran("/bin/opencode", "mcp", "auth")
    assert "opencode mcp auth" in result["agents"][2]["next_step"]


def test_dry_run_and_agent_filter_run_nothing_but_lookups():
    run = Recorder()
    result = mcp_setup.init(
        api_url="http://localhost:8000",
        agents=["codex"],
        dry_run=True,
        which=installed("claude", "codex"),
        run=run,
    )
    assert result["agents"] == [
        {
            "agent": "codex",
            "status": "would_add",
            "command": [
                "/bin/codex",
                "mcp",
                "add",
                "outcomeci",
                "--url",
                "http://localhost:8000/v1/mcp",
            ],
        }
    ]
    assert all(command[1:3] == ["mcp", "get"] for command in run.commands)


def test_default_api_url_prefers_env_then_login(tmp_path, monkeypatch):
    stored = tmp_path / "credentials.json"
    assert mcp_setup.default_api_url(stored) == "https://api.outcomeci.com"
    stored.write_text(json.dumps({"access_token": "t", "api_url": "http://localhost:8000"}))
    assert mcp_setup.default_api_url(stored) == "http://localhost:8000"
    monkeypatch.setenv("OUTCOMECI_API_URL", "https://staging.example")
    assert mcp_setup.default_api_url(stored) == "https://staging.example"


def test_cli_fails_clearly_when_no_agent_is_installed(monkeypatch, capsys):
    monkeypatch.setattr(mcp_setup.shutil, "which", lambda executable: None)
    monkeypatch.setattr(mcp_setup, "init", _init_without(mcp_setup.init))
    assert cli.main(["mcp", "init", "--api-url", "https://api.outcomeci.com"]) == 2
    assert "No supported agent found" in capsys.readouterr().err


def test_cli_prints_the_summary(monkeypatch, capsys):
    monkeypatch.setattr(mcp_setup, "init", _init_without(mcp_setup.init, installed("codex")))
    assert cli.main(["mcp", "init", "--dry-run", "--api-url", "https://api.outcomeci.com"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["agents"][1]["status"] == "would_add"


def _init_without(real, which=lambda executable: None):
    def init(**kwargs):
        return real(**kwargs, which=which, run=Recorder())

    return init
