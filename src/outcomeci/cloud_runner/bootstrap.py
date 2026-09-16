"""Initialize ECS bind mounts, then permanently drop runner privileges."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

RUNNER_UID = 10001
RUNNER_GID = 10001


def _prepare_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError(f"runner path is not a directory: {path}")
    os.chown(path, RUNNER_UID, RUNNER_GID)
    os.chmod(path, 0o700)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("runner bootstrap must start as root")
    private_root = Path(os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner"))
    work_root = Path(os.environ.get("AGENT_WORK_ROOT", "/workspace"))
    temporary_root = Path(os.environ.get("TMPDIR", "/tmp"))
    for path in (private_root, work_root, temporary_root):
        _prepare_directory(path)

    os.environ.update(
        HOME=str(private_root),
        AGENT_PRIVATE_ROOT=str(private_root),
        AGENT_WORK_ROOT=str(work_root),
        TMPDIR=str(temporary_root),
    )
    os.umask(0o077)
    os.setgroups([])
    os.setgid(RUNNER_GID)
    os.setuid(RUNNER_UID)
    os.execv(
        sys.executable,
        [sys.executable, "-m", "outcomeci.cloud_runner", *sys.argv[1:]],
    )


if __name__ == "__main__":
    main()
