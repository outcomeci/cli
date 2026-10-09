import json
from pathlib import Path

from outcomeci.artifacts import transcripts


def _write(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def _claude_line(message_id: str, model: str, output: int) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-10-09T12:00:00Z",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": 4,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 9000,
                "output_tokens": output,
            },
        },
    }


def test_claude_counts_each_response_once_with_its_model(tmp_path: Path) -> None:
    # Claude Code writes one line per content block, each with the response's usage.
    path = _write(
        tmp_path,
        [
            _claude_line("msg_1", "claude-sonnet-5", 10),
            _claude_line("msg_1", "claude-sonnet-5", 10),
            _claude_line("msg_1", "claude-sonnet-5", 42),
            {"type": "user", "message": {"role": "user", "content": "next"}},
            _claude_line("msg_2", "claude-sonnet-5", 7),
        ],
    )
    records = transcripts._usage(path, "claude")
    assert [(r["model"], r["output_tokens"], r["source_line"]) for r in records] == [
        ("claude-sonnet-5", 42, 1),
        ("claude-sonnet-5", 7, 5),
    ]
    assert records[0]["input_tokens"] == 4
    assert records[0]["cache_read_tokens"] == 9000
    assert records[0]["cache_write_tokens"] == 100


def _token_count(total_input: int, last_input: int, cached: int, output: int) -> dict:
    return {
        "type": "event_msg",
        "timestamp": "2026-10-09T12:00:00Z",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": total_input},
                "last_token_usage": {
                    "input_tokens": last_input,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": 3,
                },
            },
        },
    }


def test_codex_token_counts_skip_repeats_and_take_cache_out_of_input(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "turn_id": "t1"}},
            _token_count(1000, 1000, 800, 50),
            _token_count(1000, 1000, 800, 50),
            {"type": "turn_context", "payload": {"model": "gpt-5.5-mini", "turn_id": "t2"}},
            _token_count(1600, 600, 500, 20),
        ],
    )
    records = transcripts._usage(path, "codex")
    assert [
        (r["model"], r["input_tokens"], r["cache_read_tokens"], r["output_tokens"]) for r in records
    ] == [("gpt-5.5", 200, 800, 50), ("gpt-5.5-mini", 100, 500, 20)]


def _usage_record(response_id: str, turn_id: str, input_tokens: int, cached: int) -> dict:
    return {
        "type": "token_usage_record",
        "timestamp": "2026-10-09T12:00:00Z",
        "payload": {
            "thread_id": "th",
            "turn_id": turn_id,
            "response_id": response_id,
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": 0,
                "output_tokens": 30,
                "reasoning_output_tokens": 10,
            },
        },
    }


def test_codex_usage_records_replace_token_counts_once_per_response(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.6", "turn_id": "t1"}},
            _usage_record("resp_1", "t1", 500, 400),
            _token_count(500, 500, 400, 30),
            _usage_record("resp_1", "t1", 500, 400),
            _usage_record("resp_2", "t1", 700, 600),
        ],
    )
    records = transcripts._usage(path, "codex")
    assert [(r["model"], r["input_tokens"], r["cache_read_tokens"]) for r in records] == [
        ("gpt-5.6", 100, 400),
        ("gpt-5.6", 100, 600),
    ]


def test_opencode_uses_the_steps_model(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            {"type": "outcomeci_session", "sessionId": "ses_1", "cwd": "/work"},
            {
                "type": "step_finish",
                "timestamp": 1791558859281,
                "part": {
                    "type": "step-finish",
                    "tokens": {
                        "input": 5,
                        "output": 46,
                        "reasoning": 4,
                        "cache": {"read": 8661, "write": 139},
                    },
                },
            },
        ],
    )
    (record,) = transcripts._usage(path, "opencode", "openrouter/anthropic/claude-sonnet-4.5")
    assert record["model"] == "openrouter/anthropic/claude-sonnet-4.5"
    assert (record["input_tokens"], record["output_tokens"]) == (5, 50)


def test_usage_file_is_version_two(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    session_dir = home / ".claude" / "projects" / "p"
    session_dir.mkdir(parents=True)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    lines = [
        {"type": "user", "sessionId": "s", "cwd": str(workspace)},
        _claude_line("m", "claude-sonnet-5", 9),
    ]
    (session_dir / "s.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    outcome = tmp_path / "outcome"
    collected = transcripts._transcripts("claude", outcome, "draft", workspace=workspace)
    usage = json.loads((outcome / collected["usage_path"]).read_text())
    assert usage["schema_version"] == transcripts.USAGE_SCHEMA_VERSION == 2
    assert usage["records"][0]["model"] == "claude-sonnet-5"
