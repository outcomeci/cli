from pathlib import Path

import pytest

from outcomeci import process


def test_local_codex_can_use_existing_login(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(
        process, "command", lambda *args, **kwargs: process.Result(0, "complete", "")
    )
    assert (
        process.invoke("codex", None, "prompt", tmp_path, 10, allow_local_auth=True) == "complete"
    )


def test_container_isolated_codex_uses_fargate_as_sandbox(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: calls.append(argv) or process.Result(0, "complete", ""),
    )

    process.invoke(
        "codex",
        None,
        "prompt",
        tmp_path,
        10,
        allow_local_auth=True,
        writable_paths=[],
        container_isolated=True,
    )

    assert calls[0][:4] == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
    ]
    assert "--approve-for-me" not in calls[0]


def test_secure_execution_masks_slack_and_mounts_only_outcome_writable(
    monkeypatch, tmp_path: Path
) -> None:
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    outcome.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        process.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None
    )
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: calls.append(argv) or process.Result(0, "complete", ""),
    )
    artifact = outcome / "standup.md"
    artifact.touch()
    process.invoke(
        "codex", None, "prompt", tmp_path, 10, allow_local_auth=True, writable_paths=[artifact]
    )
    argv = calls[0]
    assert argv[:6] == [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--tmpfs",
        "/",
    ]
    assert any(
        argv[index : index + 3] == ["--bind", str(artifact), str(artifact)]
        for index in range(len(argv) - 2)
    )
    assert not any(
        argv[index : index + 3] == ["--bind", str(outcome), str(outcome)]
        for index in range(len(argv) - 2)
    )


def test_secure_execution_masks_connection_defined_credentials(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setenv("PEOPLE_TOKEN", "private")
    monkeypatch.setattr(
        process.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None
    )
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: calls.append(kwargs["env"]) or process.Result(0, "complete", ""),
    )
    process.invoke(
        "codex",
        None,
        "prompt",
        tmp_path,
        10,
        allow_local_auth=True,
        writable_paths=[],
        excluded_env={"PEOPLE_TOKEN"},
    )
    assert "PEOPLE_TOKEN" not in calls[0]


def test_secure_execution_hides_local_vault_key_file(monkeypatch, tmp_path: Path) -> None:
    calls = []
    key = tmp_path / "vault.key"
    key.write_text("private")
    monkeypatch.setenv("OUTCOMECI_VAULT_KEY_FILE", str(key))
    monkeypatch.setattr(
        process.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None
    )
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs["env"])) or process.Result(0, "complete", "")
        ),
    )
    process.invoke("codex", None, "prompt", tmp_path, 10, allow_local_auth=True, writable_paths=[])
    argv, environment = calls[0]
    assert "OUTCOMECI_VAULT_KEY_FILE" not in environment
    assert ["--ro-bind", "/dev/null", str(key)] in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]


def test_managed_codex_still_requires_injected_auth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    with pytest.raises(process.ExecutionError, match="ephemeral CODEX_HOME"):
        process.invoke("codex", None, "prompt", tmp_path, 10)


def test_opencode_uses_only_injected_openrouter_key(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs["env"])) or process.Result(0, "complete", "")
        ),
    )
    assert (
        process.invoke("opencode", "openrouter/anthropic/claude-sonnet-4", "prompt", tmp_path, 10)
        == "complete"
    )
    argv, environment = calls[0]
    assert argv == [
        "opencode",
        "run",
        "--pure",
        "--auto",
        "--format",
        "json",
        "--model",
        "openrouter/anthropic/claude-sonnet-4",
        "prompt",
    ]
    assert environment["OPENROUTER_API_KEY"] == "openrouter-secret"


def test_codex_conversation_resumes_session_read_only(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or process.Result(0, '{"message":"ok","action":null}', "")
        ),
    )
    result = process.invoke_conversation(
        "codex", "session-1", None, "question", tmp_path, 10, allow_local_auth=True
    )
    assert '"message":"ok"' in result
    assert calls[0][0] == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "resume",
        "session-1",
        "-",
    ]


def test_claude_conversation_resumes_with_read_tools(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs)) or process.Result(0, '{"message":"ok","action":null}', "")
        ),
    )
    process.invoke_conversation(
        "claude", "session-2", "claude-test", "question", tmp_path, 10, allow_local_auth=True
    )
    assert "--resume" in calls[0][0]
    assert calls[0][0][calls[0][0].index("--resume") + 1] == "session-2"
    assert "--fork-session" in calls[0][0]
    assert "Read,Grep,Glob" in calls[0][0]


def test_codex_conversation_falls_back_when_session_has_active_writer(
    monkeypatch, tmp_path: Path
) -> None:
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if "resume" in argv:
            return process.Result(1, "", "thread already has an active writer")
        return process.Result(0, '{"message":"from artifacts","action":null}', "")

    monkeypatch.setattr(process, "command", fake)
    result = process.invoke_conversation(
        "codex", "session-1", None, "question", tmp_path, 10, allow_local_auth=True
    )
    assert "from artifacts" in result
    assert "resume" in calls[0]
    assert "resume" not in calls[1]
