from unittest import mock

import pytest

from outcomeci.cloud_runner import bootstrap


def test_bootstrap_initializes_mounts_then_drops_privileges(tmp_path, monkeypatch):
    private = tmp_path / "private"
    work = tmp_path / "work"
    temporary = tmp_path / "tmp"
    monkeypatch.setenv("AGENT_PRIVATE_ROOT", str(private))
    monkeypatch.setenv("AGENT_WORK_ROOT", str(work))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(bootstrap.os, "chown", lambda path, uid, gid: calls.append(("chown", path)))
    monkeypatch.setattr(bootstrap.os, "setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr(bootstrap.os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(bootstrap.os, "setuid", lambda uid: calls.append(("uid", uid)))
    monkeypatch.setattr(bootstrap.os, "execv", lambda path, args: calls.append(("exec", args)))

    bootstrap.main()

    assert all(
        path.is_dir() and path.stat().st_mode & 0o777 == 0o700
        for path in (private, work, temporary)
    )
    assert calls[:3] == [("chown", private), ("chown", work), ("chown", temporary)]
    assert calls[3:6] == [("groups", []), ("gid", 10001), ("uid", 10001)]
    assert calls[6][0] == "exec"
    assert calls[6][1][1:3] == ["-m", "outcomeci.cloud_runner"]


def test_bootstrap_rejects_non_root(monkeypatch):
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 10001)

    with (
        mock.patch.object(bootstrap.os, "execv") as execute,
        pytest.raises(RuntimeError, match="runner bootstrap must start as root"),
    ):
        bootstrap.main()
    execute.assert_not_called()
