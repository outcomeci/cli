from pathlib import Path

import pytest

from outcomeci.runtime import process


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


def test_container_isolated_claude_bypasses_permission_prompts(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: calls.append(argv) or process.Result(0, "complete", ""),
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")

    process.invoke(
        "claude",
        None,
        "prompt",
        tmp_path,
        10,
        writable_paths=[],
        container_isolated=True,
    )

    assert calls[0][:3] == ["claude", "--print", "--dangerously-skip-permissions"]
    assert "--permission-mode" not in calls[0]


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
    artifact = outcome / "summary.md"
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


def test_claude_requires_injected_auth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(process.ExecutionError, match="ANTHROPIC_API_KEY"):
        process.invoke("claude", None, "prompt", tmp_path, 10)


def test_opencode_requires_injected_auth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(process.ExecutionError, match="OPENROUTER_API_KEY"):
        process.invoke("opencode", "openrouter/anthropic/claude-sonnet-4", "prompt", tmp_path, 10)


def test_opencode_requires_an_openrouter_prefixed_model(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    with pytest.raises(process.ExecutionError, match="openrouter/<model>"):
        process.invoke("opencode", "claude-sonnet-4", "prompt", tmp_path, 10)


def test_unsupported_agent_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(process.ExecutionError, match="unsupported agent"):
        process.invoke("gemini", None, "prompt", tmp_path, 10)


def test_opencode_conversation_requires_injected_auth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(process.ExecutionError, match="OPENROUTER_API_KEY"):
        process.invoke_conversation(
            "opencode", None, "openrouter/anthropic/claude-sonnet-4", "question", tmp_path, 10
        )


def test_claude_conversation_requires_injected_auth(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(process.ExecutionError, match="ANTHROPIC_API_KEY"):
        process.invoke_conversation("claude", None, None, "question", tmp_path, 10)


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


@pytest.mark.parametrize(
    ("container_isolated", "expected"),
    [
        (False, ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-"]),
        (
            True,
            [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "-",
            ],
        ),
    ],
)
def test_read_only_codex_uses_the_container_as_its_sandbox(
    monkeypatch, tmp_path: Path, container_isolated, expected
) -> None:
    calls = []
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: calls.append(argv) or process.Result(0, "ok", ""),
    )
    monkeypatch.setattr(process.shutil, "which", lambda name: "/usr/bin/" + name)
    process.invoke(
        "codex",
        None,
        "review",
        tmp_path,
        10,
        allow_local_auth=True,
        writable_paths=[],
        read_only=True,
        container_isolated=container_isolated,
    )
    assert calls[-1][: len(expected)] == expected or calls[-1][-len(expected) :] == expected


def test_claude_reads_its_prompt_on_stdin_however_long(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def command(argv, **kwargs):
        seen.update(argv=argv, input_text=kwargs.get("input_text"))
        return process.Result(0, "ok", "")

    monkeypatch.setattr(process, "command", command)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    prompt = "x" * 300_000
    process.invoke(
        "claude", None, prompt, tmp_path, 10, allow_local_auth=True, container_isolated=True
    )
    assert prompt not in seen["argv"]
    assert seen["input_text"] == prompt


# The shape of `opencode run --format json` output, from OpenCode 1.18.
OPENCODE_STREAM = "\n".join(
    [
        '{"type":"step_start","timestamp":1791558858000,"sessionID":"ses_1",'
        '"part":{"id":"prt_1","sessionID":"ses_1","type":"step-start"}}',
        '{"type":"text","timestamp":1791558858500,"sessionID":"ses_1",'
        '"part":{"id":"prt_2","sessionID":"ses_1","type":"text","text":"Reading the request."}}',
        '{"type":"step_finish","timestamp":1791558858600,"sessionID":"ses_1",'
        '"part":{"id":"prt_3","type":"step-finish","reason":"tool-calls",'
        '"tokens":{"total":9000,"input":1200,"output":30,"reasoning":10,'
        '"cache":{"write":0,"read":7760}},"cost":0.002}}',
        '{"type":"text","timestamp":1791558859281,"sessionID":"ses_1",'
        '"part":{"id":"prt_4","sessionID":"ses_1","type":"text",'
        '"text":"Step completed. The greeting is written."}}',
        '{"type":"step_finish","timestamp":1791558859281,"sessionID":"ses_1",'
        '"part":{"id":"prt_5","type":"step-finish","reason":"stop",'
        '"tokens":{"total":8851,"input":5,"output":46,"reasoning":0,'
        '"cache":{"write":139,"read":8661}},"cost":0.0038}}',
    ]
)


def test_opencode_returns_its_final_message_and_keeps_the_stream(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setattr(
        process, "command", lambda argv, **kwargs: process.Result(0, OPENCODE_STREAM, "")
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    summary = process.invoke(
        "opencode", "openrouter/anthropic/claude-sonnet-4", "prompt", workspace, 10
    )
    assert summary == "Step completed. The greeting is written."
    session = home / process.OPENCODE_SESSIONS / "ses_1.jsonl"
    header, *events = session.read_text().splitlines()
    assert '"cwd": "' + str(workspace) + '"' in header
    assert len(events) == 5


def test_opencode_conversation_returns_its_final_message(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setattr(
        process, "command", lambda argv, **kwargs: process.Result(0, OPENCODE_STREAM, "")
    )
    assert (
        process.invoke_conversation(
            "opencode", None, "openrouter/anthropic/claude-sonnet-4", "question", tmp_path, 10
        )
        == "Step completed. The greeting is written."
    )


def test_opencode_output_without_text_falls_back_to_the_raw_output(tmp_path: Path) -> None:
    assert process.opencode_output("plain failure text", tmp_path) == "plain failure text"


def test_opencode_step_transcript_records_usage(monkeypatch, tmp_path: Path) -> None:
    from outcomeci.artifacts import transcripts

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    process.opencode_output(OPENCODE_STREAM, workspace)
    outcome = tmp_path / "outcome"
    collected = transcripts._transcripts(
        "opencode", outcome, "greet", workspace=workspace, since="2000-01-01T00:00:00+00:00"
    )
    assert collected["usage_records"] == 2
    first, second = sorted(collected["usage"], key=lambda record: record["source_line"])
    assert (first["input_tokens"], first["output_tokens"]) == (1200, 40)
    assert (first["cache_read_tokens"], first["cache_write_tokens"]) == (7760, 0)
    assert (second["input_tokens"], second["output_tokens"]) == (5, 46)
    assert (second["cache_read_tokens"], second["cache_write_tokens"]) == (8661, 139)
    assert second["occurred_at"] == 1791558859281
    assert (outcome / collected["files"][0]["path"]).is_file()


def test_opencode_session_for_another_workspace_is_not_collected(
    monkeypatch, tmp_path: Path
) -> None:
    from outcomeci.artifacts import transcripts

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    process.opencode_output(OPENCODE_STREAM, elsewhere)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    collected = transcripts._transcripts(
        "opencode", tmp_path / "outcome", "greet", workspace=workspace
    )
    assert collected["usage_records"] == 0


def _opencode_error_event(message: str, status: int) -> str:
    """An `opencode run --format json` error event, as OpenCode 1.18 prints it."""
    import json

    return json.dumps(
        {
            "type": "error",
            "timestamp": 1791563346261,
            "sessionID": "ses_1",
            "error": {
                "name": "APIError",
                "data": {
                    "message": message,
                    "statusCode": status,
                    "isRetryable": False,
                    "responseHeaders": {
                        "set-cookie": "__cf_bm=secret-cookie; Domain=openrouter.ai",
                        "www-authenticate": 'Bearer error="invalid_token"',
                    },
                    "responseBody": json.dumps({"error": {"message": message, "code": status}}),
                    "metadata": {"url": "https://openrouter.ai/api/v1/chat/completions"},
                },
            },
        }
    )


def test_opencode_failure_reports_the_provider_reason_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: process.Result(
            1, _opencode_error_event("API key expired.", 401), ""
        ),
    )
    with pytest.raises(process.ExecutionError) as error:
        process.invoke("opencode", "openrouter/anthropic/claude-sonnet-4", "prompt", tmp_path, 10)
    assert str(error.value) == (
        "opencode failed with exit 1: API key expired. (APIError HTTP 401 from openrouter.ai)"
    )
    assert "cookie" not in str(error.value) and "invalid_token" not in str(error.value)


def test_opencode_rate_limit_still_reads_as_a_usage_limit(monkeypatch, tmp_path: Path) -> None:
    import importlib

    runner = importlib.import_module("outcomeci.cloud_runner.main")

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: process.Result(
            1, _opencode_error_event("Provider returned error", 429), ""
        ),
    )
    with pytest.raises(process.ExecutionError) as error:
        process.invoke_conversation(
            "opencode", None, "openrouter/anthropic/claude-sonnet-4", "question", tmp_path, 10
        )
    assert "HTTP 429" in str(error.value)
    assert runner._is_usage_limit_error(error.value)


def test_opencode_failure_without_an_error_event_keeps_the_raw_output(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setattr(
        process, "command", lambda argv, **kwargs: process.Result(1, "", "opencode: bad flag")
    )
    with pytest.raises(process.ExecutionError, match="exit 1: opencode: bad flag"):
        process.invoke("opencode", "openrouter/anthropic/claude-sonnet-4", "prompt", tmp_path, 10)
