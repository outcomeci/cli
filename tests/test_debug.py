from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from outcomeci import debug
from outcomeci.cloud import CloudRequestError
from outcomeci.process import ExecutionError

COMPILED = {"triggers": {"daily": {"type": "cron"}, "go": {"type": "manual"}}}


def _lease(**overrides):
    value = {
        "lease_id": "lease-1",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "values": {"slack/bot-token": {"secrets": {"value": "xoxb-secret"}}},
    }
    value.update(overrides)
    return value


def test_synthesizes_a_cron_payload_and_runs_with_the_leased_resolver(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    complete = mock.Mock()
    monkeypatch.setattr(debug, "complete_debug_lease", complete)
    captured = {}

    def trigger(root, config, name, payload, *, options):
        captured.update(name=name, payload=payload, options=options)
        assert options.credential_resolver("vault:slack/bot-token")["secrets"]["value"] == (
            "xoxb-secret"
        )
        assert options.execution_backend == "outcomeci"
        assert options._container_isolated is False
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        result = debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="daily"
        )

    assert result == {"run_id": "run-1"}
    assert captured["name"] == "daily"
    assert captured["payload"]["type"] == "cron"
    assert captured["payload"]["schema_version"] == "outcomeci.trigger.cron/v1"
    assert captured["payload"]["trigger_name"] == "daily"
    complete.assert_not_called()


def test_manual_trigger_synthesizes_an_empty_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, **options):
        assert payload == {}
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="go"
        )


def test_unsynthesizable_trigger_type_requires_a_payload_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        debug,
        "compile_workflow",
        lambda config: {"triggers": {"inbound": {"type": "webhook.received"}}},
    )
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())

    with pytest.raises(ExecutionError, match="pass --payload"):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            trigger_name="inbound",
        )


def test_payload_file_overrides_synthesis(monkeypatch, tmp_path):
    monkeypatch.setattr(
        debug,
        "compile_workflow",
        lambda config: {"triggers": {"inbound": {"type": "webhook.received"}}},
    )
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"subject": "hello"}))
    captured = {}

    def trigger(root, config, name, payload, **options):
        captured["payload"] = payload
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            trigger_name="inbound",
            payload_path=payload_file,
        )
    assert captured["payload"] == {"subject": "hello"}


def test_without_trigger_or_run_raises_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())

    with pytest.raises(ExecutionError, match="--trigger"):
        debug.run(tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1")


def test_run_mode_replays_the_real_claimed_invocation_and_reports_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(
        debug,
        "issue_debug_lease",
        lambda *a, **k: _lease(
            trigger_name="daily", trigger_type="cron", input={"type": "cron", "scheduled_at": "x"}
        ),
    )
    complete = mock.Mock()
    monkeypatch.setattr(debug, "complete_debug_lease", complete)
    captured = {}

    def trigger(root, config, name, payload, **options):
        captured.update(name=name, payload=payload)
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            invocation_id="inv-1",
        )

    assert captured["name"] == "daily"
    assert captured["payload"] == {"type": "cron", "scheduled_at": "x"}
    complete.assert_called_once_with("workspace_1", "workflow_1", "inv-1", "completed")


def test_run_mode_reports_failure_and_still_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(
        debug,
        "issue_debug_lease",
        lambda *a, **k: _lease(trigger_name="daily", trigger_type="cron", input={}),
    )
    complete = mock.Mock()
    monkeypatch.setattr(debug, "complete_debug_lease", complete)

    def trigger(root, config, name, payload, **options):
        raise ExecutionError("boom")

    with (
        mock.patch("outcomeci.local.trigger", side_effect=trigger),
        pytest.raises(ExecutionError, match="boom"),
    ):
        debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            invocation_id="inv-1",
        )

    complete.assert_called_once_with("workspace_1", "workflow_1", "inv-1", "failed")


def test_auto_continue_drives_through_ready_phases(monkeypatch, tmp_path):
    compiled = {
        "triggers": {"daily": {"type": "cron"}},
        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
    }
    monkeypatch.setattr(debug, "compile_workflow", lambda config: compiled)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())
    continue_calls = []

    def trigger(root, config, name, payload, **options):
        return {
            "run_id": "run-1",
            "status": "awaiting_confirmation",
            "completed_phases": ["resolve_analytics"],
            "ready_phases": ["notify"],
        }

    def continue_run(root, config, run_id, *, approve, **options):
        continue_calls.append((run_id, approve, options))
        return {
            "run_id": run_id,
            "status": "completed",
            "completed_phases": ["resolve_analytics", "notify"],
        }

    with (
        mock.patch("outcomeci.local.trigger", side_effect=trigger),
        mock.patch("outcomeci.local.continue_run", side_effect=continue_run),
    ):
        result = debug.run(
            tmp_path,
            tmp_path / "outcome.yml",
            "workspace_1",
            "workflow_1",
            trigger_name="daily",
            auto_continue=True,
        )

    assert result["completed_phases"] == ["resolve_analytics", "notify"]
    assert len(continue_calls) == 1
    assert continue_calls[0][0:2] == ("run-1", True)
    assert continue_calls[0][2]["options"].execution_backend == "outcomeci"
    assert continue_calls[0][2]["options"]._container_isolated is False


def test_without_auto_continue_stops_after_the_first_phase(monkeypatch, tmp_path):
    compiled = {
        "triggers": {"daily": {"type": "cron"}},
        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
    }
    monkeypatch.setattr(debug, "compile_workflow", lambda config: compiled)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, **options):
        return {
            "run_id": "run-1",
            "status": "awaiting_confirmation",
            "completed_phases": ["resolve_analytics"],
            "ready_phases": ["notify"],
        }

    with (
        mock.patch("outcomeci.local.trigger", side_effect=trigger),
        mock.patch(
            "outcomeci.local.continue_run",
            side_effect=AssertionError("continue_run should not be called"),
        ),
    ):
        result = debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="daily"
        )

    assert result["completed_phases"] == ["resolve_analytics"]


def test_resolver_rejects_a_reference_missing_from_the_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: _lease())
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, *, options):
        with pytest.raises(ExecutionError, match="not granted"):
            options.credential_resolver("vault:unknown/path")
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="go"
        )


def test_resolver_rejects_an_expired_lease(monkeypatch, tmp_path):
    monkeypatch.setattr(debug, "compile_workflow", lambda config: COMPILED)
    expired = _lease(expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    monkeypatch.setattr(debug, "issue_debug_lease", lambda *a, **k: expired)
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    def trigger(root, config, name, payload, *, options):
        with pytest.raises(ExecutionError, match="expired"):
            options.credential_resolver("vault:slack/bot-token")
        return {"run_id": "run-1"}

    with mock.patch("outcomeci.local.trigger", side_effect=trigger):
        debug.run(
            tmp_path, tmp_path / "outcome.yml", "workspace_1", "workflow_1", trigger_name="go"
        )


IMAGE_COMPILED = {
    **COMPILED,
    "workflow": {"spec": {"agents": {"default": {"runner": "codex"}}}},
    "instructions": {"phases": {"run": {}}},
}
CODEX_LOGIN = {"tokens": {"refresh_token": "rt-1"}}
ROTATED = {"tokens": {"refresh_token": "rt-2"}}


def _agent_lease(**overrides):
    return _lease(
        agent={
            "provider": "codex",
            "credential": CODEX_LOGIN,
            "credential_version": 4,
            "job_id": "job-1",
            "token": "agent-token",
        },
        **overrides,
    )


class _FakeContainer:
    """A subprocess.Popen stand-in that behaves like the debug container."""

    def __init__(self, *, returncode=0, result=None, rotated=None, interrupt=False):
        self.returncode_value = returncode
        self.result = result
        self.rotated = rotated
        self.interrupt = interrupt
        self.seen = {}

    def __call__(self, command, *, stdin, text):
        self.seen["command"] = command
        self.command = command
        self.returncode = None
        return self

    def communicate(self, stdin):
        self.seen["bundle"] = json.loads(stdin)
        mounts = [arg for arg in self.command if arg.startswith("type=bind,")]
        output = Path(
            next(m for m in mounts if "dst=/debug-out" in m).split("src=")[1].split(",dst=")[0]
        )
        self.seen["output"] = output
        if self.rotated is not None:
            codex = output / "home" / ".codex"
            codex.mkdir(parents=True)
            (codex / "auth.json").write_text(json.dumps(self.rotated))
        run = output / "work" / ".outcomeci" / "outcomes" / "run-1"
        run.mkdir(parents=True)
        (run / "run.json").write_text('{"run_id": "run-1"}')
        if self.interrupt:
            raise KeyboardInterrupt
        if self.result is not None:
            (output / "result.json").write_text(json.dumps(self.result))
        self.returncode = self.returncode_value

    def wait(self, timeout=None):
        self.returncode = -2
        return self.returncode


@pytest.fixture
def image_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(debug, "compile_workflow", lambda config: IMAGE_COMPILED)
    monkeypatch.setattr(debug, "_check_image", lambda image: None)
    monkeypatch.setattr(debug, "issue_debug_lease", mock.Mock(return_value=_agent_lease()))
    monkeypatch.setattr(debug, "complete_debug_agent_lease", mock.Mock())
    monkeypatch.setattr(debug, "renew_debug_agent_lease", mock.Mock())
    monkeypatch.setattr(debug.time, "sleep", lambda seconds: None)
    docker = mock.Mock(return_value=mock.Mock(returncode=0))
    monkeypatch.setattr(debug.subprocess, "run", docker)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "outcome.yml").write_text("")
    return root, docker


def _container(monkeypatch, **kwargs):
    container = _FakeContainer(**kwargs)
    monkeypatch.setattr(debug.subprocess, "Popen", container)
    return container


def _image_run(root, **kwargs):
    kwargs.setdefault("trigger_name", "go")
    return debug.run(
        root,
        root / "outcome.yml",
        "workspace_1",
        "workflow_1",
        image="outcomeci-runner:dev",
        **kwargs,
    )


def test_image_run_pipes_every_secret_on_stdin_and_writes_back_a_rotation(monkeypatch, image_env):
    root, _ = image_env
    container = _container(monkeypatch, result={"run_id": "run-1"}, rotated=ROTATED)

    assert _image_run(root) == {"run_id": "run-1"}

    issued = debug.issue_debug_lease
    assert issued.call_args.kwargs["agent_provider"] == "codex"
    assert issued.call_args.kwargs["ttl_seconds"] == debug.IMAGE_LEASE_TTL_SECONDS
    command = container.seen["command"]
    argv = " ".join(command)
    for secret in ("xoxb-secret", "rt-1", "agent-token"):
        assert secret not in argv
    assert command[-3:] == ["outcomeci-runner:dev", "-m", "outcomeci.debug_container"]
    assert f"type=bind,src={root.resolve()},dst=/src,readonly" in command
    assert "HOME=/debug-out/home" in command
    assert "--init" in command
    bundle = container.seen["bundle"]
    assert bundle["credential"] == CODEX_LOGIN
    assert bundle["values"]["slack/bot-token"]["secrets"]["value"] == "xoxb-secret"
    assert bundle["config"] == "outcome.yml"
    debug.complete_debug_agent_lease.assert_called_once_with(
        "workspace_1",
        "workflow_1",
        "job-1",
        "agent-token",
        "completed",
        agent_credential=ROTATED,
        expected_credential_version=4,
    )


def test_image_run_brings_the_run_state_back_without_touching_the_checkout(monkeypatch, image_env):
    root, _ = image_env
    container = _container(monkeypatch, result={"run_id": "run-1"})

    _image_run(root)

    assert (root / ".outcomeci" / "outcomes" / "run-1" / "run.json").is_file()
    assert not container.seen["output"].exists()


def test_image_run_skips_writeback_when_the_login_did_not_rotate(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1"}, rotated=CODEX_LOGIN)

    _image_run(root)

    assert debug.complete_debug_agent_lease.call_args.kwargs == {
        "agent_credential": None,
        "expected_credential_version": None,
    }


def test_image_run_releases_as_failed_when_the_run_records_an_error(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1", "status": "error"})

    _image_run(root)

    assert debug.complete_debug_agent_lease.call_args.args[4] == "failed"


def test_image_run_releases_the_lease_and_writes_back_when_the_run_fails(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, returncode=1, rotated=ROTATED)

    with pytest.raises(ExecutionError, match="failed inside outcomeci-runner:dev"):
        _image_run(root)

    assert debug.complete_debug_agent_lease.call_args.args[4] == "failed"
    assert debug.complete_debug_agent_lease.call_args.kwargs["agent_credential"] == ROTATED


def test_interrupt_stops_the_container_before_releasing_with_the_rotation(monkeypatch, image_env):
    root, docker = image_env
    container = _container(monkeypatch, rotated=ROTATED, interrupt=True)
    order = []
    docker.side_effect = lambda command, **kwargs: order.append(command[1]) or mock.Mock()
    debug.complete_debug_agent_lease.side_effect = lambda *a, **k: order.append("release")

    with pytest.raises(KeyboardInterrupt):
        _image_run(root)

    name = container.seen["command"][container.seen["command"].index("--name") + 1]
    assert docker.call_args_list[0].args[0] == ["docker", "kill", "--signal", "INT", name]
    assert order == ["kill", "rm", "release"]
    assert debug.complete_debug_agent_lease.call_args.kwargs["agent_credential"] == ROTATED


def test_input_errors_fail_before_any_lease_is_issued(monkeypatch, image_env):
    root, _ = image_env

    with pytest.raises(ExecutionError, match="does not declare trigger"):
        _image_run(root, trigger_name="missing")
    outside = root.parent / "elsewhere.yml"
    outside.write_text("")
    with pytest.raises(ExecutionError, match="must live inside --dir"):
        debug.run(root, outside, "workspace_1", "workflow_1", trigger_name="go", image="img")

    debug.issue_debug_lease.assert_not_called()


def test_a_replay_without_a_recorded_trigger_still_releases_the_agent_lease(monkeypatch, image_env):
    root, _ = image_env
    debug.issue_debug_lease.return_value = _agent_lease(trigger_name=None)
    monkeypatch.setattr(debug, "complete_debug_lease", mock.Mock())

    with pytest.raises(ExecutionError, match="no recorded trigger"):
        _image_run(root, trigger_name=None, invocation_id="inv-1")

    assert debug.complete_debug_agent_lease.call_args.args[4] == "failed"
    debug.complete_debug_lease.assert_called_once_with(
        "workspace_1", "workflow_1", "inv-1", "failed"
    )


def test_a_failed_release_keeps_the_rotation_and_does_not_mask_the_run_error(
    monkeypatch, image_env, capsys
):
    root, _ = image_env
    _container(monkeypatch, returncode=1, rotated=ROTATED)
    debug.complete_debug_agent_lease.side_effect = CloudRequestError("unreachable", None)

    with pytest.raises(ExecutionError, match="failed inside"):
        _image_run(root)

    assert debug.complete_debug_agent_lease.call_count == debug.RELEASE_ATTEMPTS
    pending = debug._pending_releases_dir() / "job-1.json"
    saved = json.loads(pending.read_text())
    assert saved["agent_credential"] == ROTATED
    assert saved["expected_credential_version"] == 4
    assert pending.stat().st_mode & 0o777 == 0o600
    err = capsys.readouterr().err
    assert str(pending) in err
    assert "rt-2" not in err

    # The next image run sends it before leasing again.
    debug.complete_debug_agent_lease.side_effect = None
    debug.complete_debug_agent_lease.reset_mock()
    _container(monkeypatch, result={"run_id": "run-1"})
    _image_run(root)

    first = debug.complete_debug_agent_lease.call_args_list[0]
    assert first.kwargs["agent_credential"] == ROTATED
    assert not pending.exists()


def test_a_refused_release_is_not_retried(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1"}, rotated=ROTATED)
    debug.complete_debug_agent_lease.side_effect = CloudRequestError("changed", 409)

    _image_run(root)

    assert debug.complete_debug_agent_lease.call_count == 1
    assert not debug._pending_releases_dir().exists()


def test_image_run_uses_the_agent_override_for_the_lease(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1"})

    _image_run(root, agent="claude")

    assert debug.issue_debug_lease.call_args.kwargs["agent_provider"] == "claude"


def test_check_image_explains_an_image_without_the_debug_entrypoint(monkeypatch):
    probe = mock.Mock(
        return_value=mock.Mock(
            returncode=1,
            stderr="ModuleNotFoundError: No module named 'outcomeci.debug_container'\n",
        )
    )
    monkeypatch.setattr(debug.subprocess, "run", probe)

    with pytest.raises(ExecutionError, match="needs an OutcomeCI runner build"):
        debug._check_image("old-runner:1")


def test_check_image_reports_missing_docker(monkeypatch):
    monkeypatch.setattr(debug.subprocess, "run", mock.Mock(side_effect=FileNotFoundError))

    with pytest.raises(ExecutionError, match="docker is not installed"):
        debug._check_image("img")


def test_import_run_state_skips_symlinks_and_odd_names(tmp_path):
    work, root = tmp_path / "work", tmp_path / "root"
    outcomes = work / ".outcomeci" / "outcomes"
    (outcomes / "run-1").mkdir(parents=True)
    (outcomes / "run-1" / "run.json").write_text("{}")
    (outcomes / "run-1" / "link").symlink_to("/etc/passwd")
    (outcomes / "..hidden").mkdir()
    (outcomes / "..hidden" / "x").write_text("")
    (work / ".outcomeci" / ".broker").symlink_to("/etc")

    debug._import_run_state(work, root)

    assert (root / ".outcomeci" / "outcomes" / "run-1" / "run.json").is_file()
    assert not (root / ".outcomeci" / "outcomes" / "run-1" / "link").exists()
    assert not (root / ".outcomeci" / "outcomes" / "..hidden").exists()
    assert not (root / ".outcomeci" / ".broker").exists()


LEASE = {"job_id": "job-1", "token": "agent-token"}


def test_heartbeat_renews_the_agent_lease_until_the_run_ends(monkeypatch):
    renewed = mock.Mock()
    monkeypatch.setattr(debug, "renew_debug_agent_lease", renewed)
    beats = threading.Event()
    renewed.side_effect = lambda *a: beats.set() if renewed.call_count >= 2 else None

    with debug._agent_lease_heartbeat("workspace_1", "workflow_1", LEASE, interval=0.01):
        assert beats.wait(5)
    count = renewed.call_count

    renewed.assert_called_with(
        "workspace_1", "workflow_1", "job-1", "agent-token", debug.AGENT_RENEW_TTL_SECONDS
    )
    time.sleep(0.05)
    assert renewed.call_count == count


def test_heartbeat_keeps_renewing_through_transient_errors(monkeypatch):
    beats = threading.Event()

    def renew(*args):
        if renewed.call_count >= 3:
            beats.set()
        raise CloudRequestError("could not reach OutcomeCI Cloud", None)

    renewed = mock.Mock(side_effect=renew)
    monkeypatch.setattr(debug, "renew_debug_agent_lease", renewed)

    with debug._agent_lease_heartbeat("workspace_1", "workflow_1", LEASE, interval=0.01):
        assert beats.wait(5)


def test_heartbeat_stops_once_the_lease_is_gone(monkeypatch, capsys):
    renewed = mock.Mock(side_effect=CloudRequestError("Debug agent lease expired", 409))
    monkeypatch.setattr(debug, "renew_debug_agent_lease", renewed)

    with debug._agent_lease_heartbeat("workspace_1", "workflow_1", LEASE, interval=0.01):
        time.sleep(0.1)

    assert renewed.call_count == 1
    assert "no longer exclusive" in capsys.readouterr().err


def test_termination_signals_interrupt_an_image_run_and_are_restored():
    import signal

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), debug._termination_interrupts():
        signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) is before
