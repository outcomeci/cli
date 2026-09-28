from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from outcomeci import debug_container
from outcomeci.process import ExecutionError


def _bundle(**overrides):
    value = {
        "config": "outcome.yml",
        "trigger": "go",
        "payload": {},
        "agent": None,
        "model": None,
        "auto_continue": False,
        "values": {"slack/bot-token": "xoxb-secret"},
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "provider": "codex",
        "credential": {"tokens": {"refresh_token": "rt-1"}},
    }
    value.update(overrides)
    return value


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_container, "compile_workflow", lambda config: {})
    monkeypatch.setattr(os, "environ", dict(os.environ))
    source, output = tmp_path / "src", tmp_path / "out"
    source.mkdir()
    (source / "outcome.yml").write_text("")
    (source / ".git").mkdir()
    output.mkdir()
    return source, output


def test_runs_on_a_private_copy_with_the_leased_credentials(monkeypatch, dirs, capsys):
    source, output = dirs
    seen = {}

    def execute(root_arg, config, compiled, name, payload, options, *, auto_continue):
        seen["root"] = root_arg
        seen["options"] = options
        seen["codex_home"] = os.environ["CODEX_HOME"]
        auth = Path(os.environ["CODEX_HOME"]) / "auth.json"
        assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "rt-1"
        (root_arg / "scratch.txt").write_text("agent output")
        return {"run_id": "run-1"}

    monkeypatch.setattr(debug_container, "execute", execute)

    code = debug_container.run_bundle(_bundle(), source=source, output=output)

    assert code == 0
    assert seen["root"] == output / "work"
    assert not (source / "scratch.txt").exists()
    assert not (output / "work" / ".git").exists()
    assert seen["options"]._container_isolated is True
    assert seen["options"].execution_backend == "outcomeci"
    assert seen["options"].credential_resolver("vault:slack/bot-token") == "xoxb-secret"
    assert seen["codex_home"] == str(output / "home" / ".codex")
    result = output / "result.json"
    assert json.loads(result.read_text()) == {"run_id": "run-1"}
    assert result.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""


def test_a_failed_run_leaves_the_codex_login_for_the_host(monkeypatch, dirs):
    source, output = dirs

    def execute(*args, **kwargs):
        raise ExecutionError("phase failed")

    monkeypatch.setattr(debug_container, "execute", execute)

    code = debug_container.run_bundle(_bundle(), source=source, output=output)

    assert code == 1
    assert not (output / "result.json").exists()
    assert (output / "home" / ".codex" / "auth.json").is_file()


def test_claude_gets_its_token(monkeypatch, dirs):
    source, output = dirs
    seen = {}

    def execute(*args, **kwargs):
        seen["token"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        return {"run_id": "run-1"}

    monkeypatch.setattr(debug_container, "execute", execute)

    bundle = _bundle(provider="claude", credential="claude-oauth-token")
    assert debug_container.run_bundle(bundle, source=source, output=output) == 0

    assert seen["token"] == "claude-oauth-token"
    assert not (output / "home" / ".codex").exists()


def test_an_unsupported_provider_fails_cleanly(monkeypatch, dirs, capsys):
    source, output = dirs
    monkeypatch.setattr(debug_container, "execute", mock.Mock())

    assert debug_container.run_bundle(_bundle(provider="gemini"), source=source, output=output) == 1
    assert "unsupported workflow agent" in capsys.readouterr().err


@pytest.mark.parametrize("stdin", ["", "not json", "[]", json.dumps({"provider": "codex"})])
def test_main_rejects_a_malformed_bundle(monkeypatch, capsys, stdin):
    monkeypatch.setattr(debug_container.sys, "stdin", io.StringIO(stdin))
    run_bundle = mock.Mock()
    monkeypatch.setattr(debug_container, "run_bundle", run_bundle)

    assert debug_container.main() == 2
    assert "invalid debug bundle" in capsys.readouterr().err
    run_bundle.assert_not_called()


def test_main_runs_the_bundle_from_the_container_mounts(monkeypatch):
    monkeypatch.setattr(debug_container.sys, "stdin", io.StringIO(json.dumps(_bundle())))
    run_bundle = mock.Mock(return_value=0)
    monkeypatch.setattr(debug_container, "run_bundle", run_bundle)

    assert debug_container.main() == 0
    assert run_bundle.call_args.kwargs == {
        "source": Path("/src"),
        "output": Path("/debug-out"),
    }


def test_main_exits_quietly_when_the_host_stops_the_run(monkeypatch, capsys):
    monkeypatch.setattr(debug_container.sys, "stdin", io.StringIO(json.dumps(_bundle())))
    monkeypatch.setattr(debug_container, "run_bundle", mock.Mock(side_effect=KeyboardInterrupt))

    assert debug_container.main() == 130
    assert "interrupted" in capsys.readouterr().err
