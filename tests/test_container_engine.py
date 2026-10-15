from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from outcomeci.runtime import container_engine as engines
from outcomeci.runtime.process import ExecutionError


def _info(monkeypatch, installed, info):
    monkeypatch.setattr(engines.shutil, "which", lambda name: name if name in installed else None)
    probe = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(info), ""))
    monkeypatch.setattr(engines.subprocess, "run", probe)
    return probe


@pytest.mark.parametrize("command", ["podman", "docker"])
def test_rootless_podman_and_docker_shim_preserve_host_ownership(monkeypatch, command):
    _info(
        monkeypatch, [command], {"host": {"security": {"rootless": True, "selinuxEnabled": True}}}
    )
    engine = engines.discover_engine()
    assert engine.command == command
    assert engine.podman and engine.selinux
    assert engine.user_args() == ["--user", f"{os.getuid()}:{os.getgid()}", "--userns=keep-id"]


def test_docker_is_preferred_and_podman_can_be_selected(monkeypatch):
    probe = _info(monkeypatch, ["docker", "podman"], {"SecurityOptions": ["name=selinux"]})
    engine = engines.discover_engine()
    assert engine.command == "docker" and engine.selinux
    assert not engine.podman
    assert "--userns=keep-id" not in engine.user_args()
    engines.discover_engine("podman")
    assert probe.call_args.args[0][0] == "podman"


def test_rootful_podman_does_not_request_rootless_mapping(monkeypatch):
    _info(monkeypatch, ["podman"], {"host": {"security": {"rootless": False}}})
    assert "--userns=keep-id" not in engines.discover_engine().user_args()


def test_missing_engine_has_installation_guidance(monkeypatch):
    _info(monkeypatch, [], {})
    with pytest.raises(ExecutionError, match="install a container engine"):
        engines.discover_engine()


def test_unavailable_docker_does_not_silently_switch_to_another_engine(monkeypatch):
    probe = _info(monkeypatch, ["docker", "podman"], {})
    probe.return_value = subprocess.CompletedProcess([], 1, "", "permission denied")
    with pytest.raises(ExecutionError, match="permission denied.*--runtime podman"):
        engines.discover_engine()
    assert probe.call_count == 1


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("docker", 30), OSError("missing")])
def test_runtime_probe_failures_are_actionable(monkeypatch, failure):
    probe = _info(monkeypatch, ["docker"], {})
    probe.side_effect = failure
    with pytest.raises(ExecutionError, match="check `docker info`"):
        engines.discover_engine()


@pytest.mark.parametrize("raw", ["not json", "null", "[]"])
def test_invalid_runtime_info_is_an_execution_error(monkeypatch, raw):
    probe = _info(monkeypatch, ["docker"], {})
    probe.return_value.stdout = raw
    with pytest.raises(ExecutionError, match="cannot read docker info"):
        engines.discover_engine()


def test_selinux_relabels_only_disposable_copies_and_cleans_them_on_failure(tmp_path):
    source, output = tmp_path / "checkout", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "outcome.yml").write_text("workflow")
    (source / ".git").mkdir()
    (source / "link").symlink_to("outcome.yml")
    with (
        pytest.raises(RuntimeError),
        engines.ContainerEngine(selinux=True).mounts(source, output) as args,
    ):
        assert args[0] == args[2] == "--volume"
        staging = Path(args[1].removesuffix(":/src:ro,Z"))
        assert staging != source
        assert (staging / "outcome.yml").read_text() == "workflow"
        assert (staging / "link").is_symlink()
        assert not (staging / ".git").exists()
        assert args[3] == f"{output}:/oci-run:Z"
        raise RuntimeError("interrupted")
    assert not staging.exists()
    assert (source / "outcome.yml").read_text() == "workflow"
    assert (source / ".git").is_dir()


def test_without_selinux_the_source_is_mounted_read_only(tmp_path):
    with engines.ContainerEngine().mounts(tmp_path, tmp_path / "output") as args:
        assert args == [
            "--mount",
            f"type=bind,src={tmp_path},dst=/src,readonly",
            "--mount",
            f"type=bind,src={tmp_path}/output,dst=/oci-run",
        ]
