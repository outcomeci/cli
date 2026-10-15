"""Discover the local container engine and preserve host file ownership and labels."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from outcomeci.runtime.process import ExecutionError


@dataclass(frozen=True)
class ContainerEngine:
    command: str = "docker"
    podman: bool = False
    rootless: bool = False
    selinux: bool = False
    shared_temp: Path | None = None

    def temporary_directory(self, *, prefix: str) -> tempfile.TemporaryDirectory[str]:
        """Keep scratch mounts visible to Desktop engines' separate VM."""
        try:
            return tempfile.TemporaryDirectory(prefix=prefix, dir=self.shared_temp)
        except OSError as exc:
            raise ExecutionError(
                f"cannot create container scratch directory in "
                f"{self.shared_temp or tempfile.gettempdir()}: {exc}"
            ) from exc

    def user_args(self) -> list[str]:
        args = ["--user", f"{os.getuid()}:{os.getgid()}"]
        if self.podman and self.rootless:
            # Otherwise the host uid is mapped to a subordinate uid, which
            # cannot open the private (0700) run directory.
            args += ["--userns=keep-id"]
        return args

    @contextmanager
    def mounts(self, source: Path, output: Path) -> Iterator[list[str]]:
        if not self.selinux and self.shared_temp is None:
            yield [
                "--mount",
                f"type=bind,src={source},dst=/src,readonly",
                "--mount",
                f"type=bind,src={output},dst=/oci-run",
            ]
            return
        # Stage outside the checkout: SELinux can safely relabel this copy,
        # and Desktop engines can see it even if the checkout is not shared.
        with self.temporary_directory(prefix="oci-source-") as staging:
            shutil.copytree(
                source,
                staging,
                dirs_exist_ok=True,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git"),
            )
            if self.selinux:
                yield ["--volume", f"{staging}:/src:ro,Z", "--volume", f"{output}:/oci-run:Z"]
            else:
                yield [
                    "--mount",
                    f"type=bind,src={staging},dst=/src,readonly",
                    "--mount",
                    f"type=bind,src={output},dst=/oci-run",
                ]


def discover_engine(requested: str | None = None) -> ContainerEngine:
    command = requested or next((name for name in ("docker", "podman") if shutil.which(name)), None)
    if command is None or shutil.which(command) is None:
        raise ExecutionError(
            f"{requested or 'Docker or Podman'} is not installed or not on PATH; "
            "install a container engine on the machine running oci"
        )
    try:
        probe = subprocess.run(
            [command, "info", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError(f"cannot contact {command}; check `{command} info`: {exc}") from exc
    if probe.returncode:
        detail = probe.stderr.strip() or probe.stdout.strip()
        raise ExecutionError(
            f"{command} is unavailable: {detail}; check `{command} info` "
            "or select another installed engine with --runtime docker or --runtime podman"
        )
    try:
        info = json.loads(probe.stdout)
        # podman-docker reports Podman's info, even when invoked as `docker`.
        podman = isinstance(info.get("host"), dict)
        security = info["host"].get("security", {}) if podman else {}
        options = info.get("SecurityOptions") or []
        return ContainerEngine(
            command=command,
            podman=podman,
            rootless=bool(security.get("rootless")),
            selinux=bool(security.get("selinuxEnabled"))
            or any(option == "name=selinux" for option in options),
            # On macOS/Linux Rancher's Lima VM shares home and this directory,
            # not the host's general /tmp (nor macOS's per-user temp directory).
            shared_temp=Path("/tmp/rancher-desktop")
            if info.get("Name") == "lima-rancher-desktop"
            else None,
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ExecutionError(f"cannot read {command} info; check `{command} info`") from exc
