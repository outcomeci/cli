from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from outcomeci import run_container, workflow_run
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


def test_a_cron_trigger_gets_a_synthesized_payload():
    name, payload = workflow_run._resolve_trigger(COMPILED, "daily", None)
    assert name == "daily"
    assert payload["type"] == "cron"
    assert payload["schema_version"] == "outcomeci.trigger.cron/v1"
    assert payload["trigger_name"] == "daily"


def test_a_manual_trigger_gets_an_empty_payload():
    assert workflow_run._resolve_trigger(COMPILED, "go", None) == ("go", {})


def test_a_trigger_without_a_synthesized_payload_needs_a_payload_file():
    compiled = {"triggers": {"inbound": {"type": "webhook.received"}}}
    with pytest.raises(ExecutionError, match="pass --payload"):
        workflow_run._resolve_trigger(compiled, "inbound", None)


def test_a_payload_file_overrides_synthesis(tmp_path):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps({"subject": "hello"}))
    compiled = {"triggers": {"inbound": {"type": "webhook.received"}}}
    assert workflow_run._resolve_trigger(compiled, "inbound", payload_file) == (
        "inbound",
        {"subject": "hello"},
    )


def test_the_default_trigger_is_the_only_one_or_the_manual_one():
    assert workflow_run._default_trigger(COMPILED) == "go"
    assert workflow_run._default_trigger({"triggers": {"daily": {"type": "cron"}}}) == "daily"
    with pytest.raises(ExecutionError, match="pass --trigger"):
        workflow_run._default_trigger(
            {"triggers": {"a": {"type": "cron"}, "b": {"type": "email.received"}}}
        )


def test_the_lease_resolver_refuses_an_ungranted_reference():
    resolver = run_container._lease_resolver(_lease()["values"], _lease()["expires_at"])
    assert resolver("vault:slack/bot-token")["secrets"]["value"] == "xoxb-secret"
    with pytest.raises(ExecutionError, match="not granted"):
        resolver("vault:unknown/path")


def test_the_lease_resolver_refuses_an_expired_lease():
    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    resolver = run_container._lease_resolver(_lease()["values"], expired)
    with pytest.raises(ExecutionError, match="expired"):
        resolver("vault:slack/bot-token")


def _two_steps(monkeypatch):
    compiled = {
        "triggers": {"daily": {"type": "cron"}},
        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
    }
    first = {
        "run_id": "run-1",
        "status": "awaiting_confirmation",
        "completed_steps": ["resolve_analytics"],
        "ready_steps": ["notify"],
    }
    monkeypatch.setattr("outcomeci.local.trigger", lambda *args, **kwargs: first)
    return compiled


def test_auto_continue_drives_through_ready_steps(monkeypatch, tmp_path):
    compiled = _two_steps(monkeypatch)
    calls = []

    def continue_run(root, config, run_id, *, approve, options):
        calls.append((run_id, approve, options))
        return {"run_id": run_id, "status": "completed", "completed_steps": ["a", "b"]}

    monkeypatch.setattr("outcomeci.local.continue_run", continue_run)
    options = object()
    result = run_container.execute(
        tmp_path, tmp_path / "outcome.yml", compiled, "daily", {}, options, auto_continue=True
    )

    assert result["completed_steps"] == ["a", "b"]
    assert calls == [("run-1", True, options)]


def test_without_auto_continue_the_run_stops_after_the_first_step(monkeypatch, tmp_path):
    compiled = _two_steps(monkeypatch)
    monkeypatch.setattr(
        "outcomeci.local.continue_run", mock.Mock(side_effect=AssertionError("continued"))
    )
    result = run_container.execute(
        tmp_path, tmp_path / "outcome.yml", compiled, "daily", {}, object(), auto_continue=False
    )
    assert result["completed_steps"] == ["resolve_analytics"]


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
    """A subprocess.Popen stand-in that behaves like the run container."""

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
            next(m for m in mounts if "dst=/oci-run" in m).split("src=")[1].split(",dst=")[0]
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
    monkeypatch.setattr(workflow_run, "compile_workflow", lambda config: IMAGE_COMPILED)
    monkeypatch.setattr(workflow_run, "_check_image", lambda image: None)
    monkeypatch.setattr(workflow_run, "issue_debug_lease", mock.Mock(return_value=_agent_lease()))
    monkeypatch.setattr(workflow_run, "complete_debug_agent_lease", mock.Mock())
    monkeypatch.setattr(workflow_run, "renew_debug_agent_lease", mock.Mock())
    monkeypatch.setattr(workflow_run.time, "sleep", lambda seconds: None)
    docker = mock.Mock(return_value=mock.Mock(returncode=0))
    monkeypatch.setattr(workflow_run.subprocess, "run", docker)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "outcome.yml").write_text("")
    return root, docker


def _container(monkeypatch, **kwargs):
    container = _FakeContainer(**kwargs)
    monkeypatch.setattr(workflow_run.subprocess, "Popen", container)
    return container


def _image_run(root, **kwargs):
    kwargs.setdefault("trigger_name", "go")
    return workflow_run.run_cloud(
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

    issued = workflow_run.issue_debug_lease
    assert issued.call_args.kwargs["agent_provider"] == "codex"
    assert issued.call_args.kwargs["ttl_seconds"] == workflow_run.IMAGE_LEASE_TTL_SECONDS
    command = container.seen["command"]
    argv = " ".join(command)
    for secret in ("xoxb-secret", "rt-1", "agent-token"):
        assert secret not in argv
    assert command[-3:] == ["outcomeci-runner:dev", "-m", "outcomeci.run_container"]
    assert f"type=bind,src={root.resolve()},dst=/src,readonly" in command
    assert "HOME=/oci-run/home" in command
    assert "--init" in command
    bundle = container.seen["bundle"]
    assert bundle["credentials"] == [{"provider": "codex", "credential": CODEX_LOGIN}]
    assert bundle["values"]["slack/bot-token"]["secrets"]["value"] == "xoxb-secret"
    assert bundle["config"] == "outcome.yml"
    workflow_run.complete_debug_agent_lease.assert_called_once_with(
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

    assert workflow_run.complete_debug_agent_lease.call_args.kwargs == {
        "agent_credential": None,
        "expected_credential_version": None,
    }


def test_image_run_releases_as_failed_when_the_run_records_an_error(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1", "status": "error"})

    _image_run(root)

    assert workflow_run.complete_debug_agent_lease.call_args.args[4] == "failed"


def test_image_run_releases_the_lease_and_writes_back_when_the_run_fails(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, returncode=1, rotated=ROTATED)

    with pytest.raises(ExecutionError, match="failed inside outcomeci-runner:dev"):
        _image_run(root)

    assert workflow_run.complete_debug_agent_lease.call_args.args[4] == "failed"
    assert workflow_run.complete_debug_agent_lease.call_args.kwargs["agent_credential"] == ROTATED


def test_interrupt_stops_the_container_before_releasing_with_the_rotation(monkeypatch, image_env):
    root, docker = image_env
    container = _container(monkeypatch, rotated=ROTATED, interrupt=True)
    order = []
    docker.side_effect = lambda command, **kwargs: order.append(command[1]) or mock.Mock()
    workflow_run.complete_debug_agent_lease.side_effect = lambda *a, **k: order.append("release")

    with pytest.raises(KeyboardInterrupt):
        _image_run(root)

    name = container.seen["command"][container.seen["command"].index("--name") + 1]
    assert docker.call_args_list[0].args[0] == ["docker", "kill", "--signal", "INT", name]
    assert order == ["kill", "rm", "release"]
    assert workflow_run.complete_debug_agent_lease.call_args.kwargs["agent_credential"] == ROTATED


def test_input_errors_fail_before_any_lease_is_issued(monkeypatch, image_env):
    root, _ = image_env

    with pytest.raises(ExecutionError, match="does not declare trigger"):
        _image_run(root, trigger_name="missing")
    outside = root.parent / "elsewhere.yml"
    outside.write_text("")
    with pytest.raises(ExecutionError, match="must live inside --dir"):
        workflow_run.run_cloud(
            root, outside, "workspace_1", "workflow_1", trigger_name="go", image="img"
        )

    workflow_run.issue_debug_lease.assert_not_called()


def test_a_failed_release_keeps_the_rotation_and_does_not_mask_the_run_error(
    monkeypatch, image_env, capsys
):
    root, _ = image_env
    _container(monkeypatch, returncode=1, rotated=ROTATED)
    workflow_run.complete_debug_agent_lease.side_effect = CloudRequestError("unreachable", None)

    with pytest.raises(ExecutionError, match="failed inside"):
        _image_run(root)

    assert workflow_run.complete_debug_agent_lease.call_count == workflow_run.RELEASE_ATTEMPTS
    pending = workflow_run._pending_releases_dir() / "job-1.json"
    saved = json.loads(pending.read_text())
    assert saved["agent_credential"] == ROTATED
    assert saved["expected_credential_version"] == 4
    assert pending.stat().st_mode & 0o777 == 0o600
    err = capsys.readouterr().err
    assert str(pending) in err
    assert "rt-2" not in err

    # The next image run sends it before leasing again.
    workflow_run.complete_debug_agent_lease.side_effect = None
    workflow_run.complete_debug_agent_lease.reset_mock()
    _container(monkeypatch, result={"run_id": "run-1"})
    _image_run(root)

    first = workflow_run.complete_debug_agent_lease.call_args_list[0]
    assert first.kwargs["agent_credential"] == ROTATED
    assert not pending.exists()


def test_a_refused_release_is_not_retried(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1"}, rotated=ROTATED)
    workflow_run.complete_debug_agent_lease.side_effect = CloudRequestError("changed", 409)

    _image_run(root)

    assert workflow_run.complete_debug_agent_lease.call_count == 1
    assert not workflow_run._pending_releases_dir().exists()


def test_image_run_uses_the_agent_override_for_the_lease(monkeypatch, image_env):
    root, _ = image_env
    _container(monkeypatch, result={"run_id": "run-1"})

    _image_run(root, agent="claude")

    assert workflow_run.issue_debug_lease.call_args.kwargs["agent_provider"] == "claude"


def test_check_image_explains_an_image_without_the_run_entrypoint(monkeypatch):
    probe = mock.Mock(
        return_value=mock.Mock(
            returncode=1,
            stderr="ModuleNotFoundError: No module named 'outcomeci.run_container'\n",
        )
    )
    monkeypatch.setattr(workflow_run.subprocess, "run", probe)

    with pytest.raises(ExecutionError, match="needs an OutcomeCI runner build"):
        workflow_run._check_image("old-runner:1")


def test_check_image_reports_missing_docker(monkeypatch):
    monkeypatch.setattr(workflow_run.subprocess, "run", mock.Mock(side_effect=FileNotFoundError))

    with pytest.raises(ExecutionError, match="docker is not installed"):
        workflow_run._check_image("img")


def test_import_run_state_skips_symlinks_and_odd_names(tmp_path):
    work, root = tmp_path / "work", tmp_path / "root"
    outcomes = work / ".outcomeci" / "outcomes"
    (outcomes / "run-1").mkdir(parents=True)
    (outcomes / "run-1" / "run.json").write_text("{}")
    (outcomes / "run-1" / "link").symlink_to("/etc/passwd")
    (outcomes / "..hidden").mkdir()
    (outcomes / "..hidden" / "x").write_text("")
    (work / ".outcomeci" / ".broker").symlink_to("/etc")

    workflow_run._import_run_state(work, root)

    assert (root / ".outcomeci" / "outcomes" / "run-1" / "run.json").is_file()
    assert not (root / ".outcomeci" / "outcomes" / "run-1" / "link").exists()
    assert not (root / ".outcomeci" / "outcomes" / "..hidden").exists()
    assert not (root / ".outcomeci" / ".broker").exists()


LEASE = {"job_id": "job-1", "token": "agent-token"}


def test_heartbeat_renews_the_agent_lease_until_the_run_ends(monkeypatch):
    renewed = mock.Mock()
    monkeypatch.setattr(workflow_run, "renew_debug_agent_lease", renewed)
    beats = threading.Event()
    renewed.side_effect = lambda *a: beats.set() if renewed.call_count >= 2 else None

    with workflow_run._agent_lease_heartbeat("workspace_1", "workflow_1", LEASE, interval=0.01):
        assert beats.wait(5)
    count = renewed.call_count

    renewed.assert_called_with(
        "workspace_1", "workflow_1", "job-1", "agent-token", workflow_run.AGENT_RENEW_TTL_SECONDS
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
    monkeypatch.setattr(workflow_run, "renew_debug_agent_lease", renewed)

    with workflow_run._agent_lease_heartbeat("workspace_1", "workflow_1", LEASE, interval=0.01):
        assert beats.wait(5)


def test_heartbeat_stops_once_the_lease_is_gone(monkeypatch, capsys):
    renewed = mock.Mock(side_effect=CloudRequestError("Debug agent lease expired", 409))
    monkeypatch.setattr(workflow_run, "renew_debug_agent_lease", renewed)

    with workflow_run._agent_lease_heartbeat("workspace_1", "workflow_1", LEASE, interval=0.01):
        time.sleep(0.1)

    assert renewed.call_count == 1
    assert "no longer exclusive" in capsys.readouterr().err


def test_termination_signals_interrupt_an_image_run_and_are_restored():
    import signal

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), workflow_run._termination_interrupts():
        signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) is before


def test_image_run_joins_the_requested_docker_network(monkeypatch, image_env):
    root, _ = image_env
    container = _container(monkeypatch, result={"run_id": "run-1"})

    _image_run(root, network="host")

    command = container.seen["command"]
    assert command[command.index("--network") + 1] == "host"
    assert command.index("--network") < command.index("outcomeci-runner:dev")


STEP_RUNNERS = {
    **IMAGE_COMPILED,
    "instructions": {
        "phases": {
            "draft": {"policy": {"runner": "codex"}},
            "implement": {"policy": {"runner": "claude"}},
        }
    },
}


def _claude_lease():
    return _lease(
        agent={
            "provider": "claude",
            "credential": "claude-oauth-token",
            "credential_version": 1,
            "job_id": "job-2",
            "token": "claude-token",
        }
    )


def test_every_runner_a_step_selects_gets_its_own_lease(monkeypatch, image_env):
    root, _ = image_env
    monkeypatch.setattr(workflow_run, "compile_workflow", lambda config: STEP_RUNNERS)
    workflow_run.issue_debug_lease.side_effect = [_agent_lease(), _claude_lease()]
    container = _container(monkeypatch, result={"run_id": "run-1"})

    _image_run(root)

    providers = [
        call.kwargs["agent_provider"] for call in workflow_run.issue_debug_lease.call_args_list
    ]
    assert providers == ["codex", "claude"]
    assert [item["provider"] for item in container.seen["bundle"]["credentials"]] == [
        "codex",
        "claude",
    ]
    released = [call.args[2] for call in workflow_run.complete_debug_agent_lease.call_args_list]
    assert released == ["job-1", "job-2"]


def test_a_refused_second_lease_releases_the_first(monkeypatch, image_env):
    root, _ = image_env
    monkeypatch.setattr(workflow_run, "compile_workflow", lambda config: STEP_RUNNERS)
    workflow_run.issue_debug_lease.side_effect = [_agent_lease(), CloudRequestError("busy", 409)]

    with pytest.raises(CloudRequestError, match="busy"):
        _image_run(root)

    assert [call.args[2] for call in workflow_run.complete_debug_agent_lease.call_args_list] == [
        "job-1"
    ]


def test_retry_resumes_a_failed_run_in_the_image(monkeypatch, image_env):
    root, _ = image_env
    record = root / ".outcomeci" / "outcomes" / "run-1" / "run.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"run_id": "run-1", "status": "error"}))
    container = _container(monkeypatch, result={"run_id": "run-1", "status": "completed"})

    _image_run(root, trigger_name=None, retry_run="run-1")

    assert container.seen["bundle"]["retry"] == "run-1"
    assert container.seen["bundle"]["trigger"] is None


def test_retry_refuses_a_run_that_did_not_fail(monkeypatch, image_env):
    root, _ = image_env
    record = root / ".outcomeci" / "outcomes" / "run-1" / "run.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"run_id": "run-1", "status": "completed"}))

    with pytest.raises(ExecutionError, match="only a failed or interrupted run retries"):
        _image_run(root, trigger_name=None, retry_run="run-1")
    workflow_run.issue_debug_lease.assert_not_called()


def test_resume_records_an_interrupted_run_before_retrying(monkeypatch, tmp_path):
    from outcomeci import local

    local._write(tmp_path, {"run_id": "run-1", "status": "running", "step": "implement"})
    retried = mock.Mock(return_value={"run_id": "run-1", "status": "completed"})
    monkeypatch.setattr(local, "retry", retried)

    run_container.resume(
        tmp_path,
        tmp_path / "w.yaml",
        {"instructions": {"phases": {}}},
        "run-1",
        None,
        auto_continue=False,
    )

    assert local._read(tmp_path, "run-1")["error"] == "the run was interrupted"
    retried.assert_called_once()


LOCAL_COMPILED = {
    **IMAGE_COMPILED,
    "workflow": {
        "spec": {
            "agents": {"default": {"runner": "codex"}},
            "connections": [{"name": "github", "auth": {"credential": "vault:github"}}],
        }
    },
}


@pytest.fixture
def local_env(monkeypatch, image_env, tmp_path):
    from outcomeci import local_vault

    root, docker = image_env
    monkeypatch.setattr(workflow_run, "compile_workflow", lambda config: LOCAL_COMPILED)
    local_vault.initialize(root)
    local_vault.put(root, "github", "ghp-secret")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps(CODEX_LOGIN))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return root, codex_home


def _local_run(root, **kwargs):
    kwargs.setdefault("image", "outcomeci-runner:dev")
    return workflow_run.run_local(root, root / "outcome.yml", **kwargs)


def test_local_run_sends_local_vault_values_and_the_local_login(monkeypatch, local_env):
    root, _ = local_env
    container = _container(monkeypatch, result={"run_id": "run-1"})

    assert _local_run(root) == {"run_id": "run-1"}

    bundle = container.seen["bundle"]
    assert bundle["values"] == {"github": "ghp-secret"}
    assert bundle["credentials"] == [{"provider": "codex", "credential": CODEX_LOGIN}]
    assert bundle["trigger"] == "go"
    assert bundle["payload"] == {}
    assert "ghp-secret" not in " ".join(container.seen["command"])
    assert (root / ".outcomeci" / "outcomes" / "run-1" / "run.json").is_file()
    workflow_run.issue_debug_lease.assert_not_called()


def test_local_run_writes_a_rotated_codex_login_back(monkeypatch, local_env):
    root, codex_home = local_env
    _container(monkeypatch, result={"run_id": "run-1"}, rotated=ROTATED)

    _local_run(root)

    assert json.loads((codex_home / "auth.json").read_text()) == ROTATED


def test_local_run_writes_back_a_rotation_even_when_the_run_fails(monkeypatch, local_env):
    root, codex_home = local_env
    _container(monkeypatch, returncode=1, rotated=ROTATED)

    with pytest.raises(ExecutionError):
        _local_run(root)

    assert json.loads((codex_home / "auth.json").read_text()) == ROTATED


def test_local_run_names_the_missing_vault_entry(monkeypatch, local_env):
    root, _ = local_env
    missing = {
        **LOCAL_COMPILED,
        "workflow": {
            "spec": {
                **LOCAL_COMPILED["workflow"]["spec"],
                "connections": [{"auth": {"credential": "vault:slack/bot-token"}}],
            }
        },
    }
    monkeypatch.setattr(workflow_run, "compile_workflow", lambda config: missing)

    with pytest.raises(ExecutionError, match="oci vault local put slack/bot-token"):
        _local_run(root)


def test_local_run_needs_a_codex_login(monkeypatch, local_env, tmp_path):
    root, _ = local_env
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))

    with pytest.raises(ExecutionError, match="codex login"):
        _local_run(root)


def test_local_run_reads_a_claude_token_from_the_environment_or_the_vault(monkeypatch, local_env):
    from outcomeci import local_vault

    root, _ = local_env
    container = _container(monkeypatch, result={"run_id": "run-1"})
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(ExecutionError, match="agents/claude"):
        _local_run(root, agent="claude")

    local_vault.put(root, "agents/claude", "vault-token")
    _local_run(root, agent="claude")
    assert container.seen["bundle"]["credentials"] == [
        {"provider": "claude", "credential": "vault-token"}
    ]

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-token")
    _local_run(root, agent="claude")
    assert container.seen["bundle"]["credentials"][0]["credential"] == "env-token"


def test_local_run_defaults_to_the_manual_trigger_and_the_released_image(monkeypatch, local_env):
    root, _ = local_env
    container = _container(monkeypatch, result={"run_id": "run-1"})
    monkeypatch.setattr("outcomeci.__version__", "1.2.3")

    workflow_run.run_local(root, root / "outcome.yml")

    assert container.seen["bundle"]["trigger"] == "go"
    assert "ghcr.io/outcomeci/outcome-runner:1.2.3" in container.seen["command"]


def test_local_run_refuses_to_guess_an_image_for_a_development_build(monkeypatch, local_env):
    root, _ = local_env
    monkeypatch.setattr("outcomeci.__version__", "1.2.4.dev3+gabc")

    with pytest.raises(ExecutionError, match="pass --image"):
        workflow_run.run_local(root, root / "outcome.yml")


def test_local_run_accepts_opencode_with_an_openrouter_model(monkeypatch, local_env):
    root, _ = local_env
    container = _container(monkeypatch, result={"run_id": "run-1"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    _local_run(root, agent="opencode", model="openrouter/anthropic/claude-sonnet-5")

    bundle = container.seen["bundle"]
    assert bundle["agent"] == "opencode"
    assert bundle["model"] == "openrouter/anthropic/claude-sonnet-5"
    assert bundle["credentials"] == [{"provider": "opencode", "credential": "or-key"}]


def test_local_run_reads_an_opencode_key_from_the_local_vault(monkeypatch, local_env):
    from outcomeci import local_vault

    root, _ = local_env
    container = _container(monkeypatch, result={"run_id": "run-1"})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    local_vault.put(root, "agents/opencode", "vault-or-key")

    _local_run(root, agent="opencode", model="openrouter/openai/gpt-5.5")

    assert container.seen["bundle"]["credentials"][0]["credential"] == "vault-or-key"


def test_opencode_needs_an_openrouter_model(monkeypatch, local_env):
    root, _ = local_env
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    for model in (None, "gpt-5.5"):
        with pytest.raises(ExecutionError, match="--model openrouter/"):
            _local_run(root, agent="opencode", model=model)


def test_the_cli_offers_opencode_for_agent():
    from outcomeci.cli import AGENT_CHOICES

    assert "opencode" in AGENT_CHOICES
