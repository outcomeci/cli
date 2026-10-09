"""Run directories become dataset rows, and the check says what each lacks."""

import json
from pathlib import Path

from outcomeci import cloud
from outcomeci.artifacts import dataset

RUN_STATE = {
    "run_id": "run-1",
    "status": "completed",
    "completed_steps": ["scan", "share"],
    "trigger": {"type": "cron", "scheduled_at": "2026-10-07T15:00:00+00:00"},
    "workflow_revision": "abc",
    "usage_records": [],
}


def _files() -> dict[str, bytes]:
    return {
        "run.json": json.dumps(RUN_STATE).encode(),
        "manifest.json": b'{"artifacts": []}',
        "effects.json": b'{"effects": []}',
        "calls.json": b'{"schema_version": "outcomeci.calls/v1", "calls": [], "events": []}',
        "policy.json": b'{"schema_version": "outcomeci.policy/v1", "decisions": []}',
        "scan/outputs.json": b'{"posts": []}',
        "share/outputs.json": b'{"posted": true}',
        "share/items/0.json": b'{"posted": true, "priority": 1}',
        "transcripts/scan/model/01-turns.jsonl": b'{"turn": 1}\n{"turn": 2}\n',
        "transcripts/scan/usage.json": b'{"input_tokens": 10}',
        "model-turns/share/turn-1.json": b'{"step": "share", "status": "succeeded"}',
        "attachments/photo.bin": bytes(range(16)),
    }


def test_model_turns_are_recognised_in_both_storage_layouts() -> None:
    assert dataset.model_turn_step("model-turns/scan/abc.json") == "scan"
    assert (
        dataset.model_turn_step(
            "wso_1653d0c4ee901dedc92f60000b353cc8/model-turns_scan_personal_fd325fc1-68f2-4f04-8be0-20f9315f40ad.json"
        )
        == "scan_personal"
    )
    assert dataset.model_turn_step("scan/outputs.json") is None
    files = {
        "run.json": b"{}",
        "wso_x/model-turns_share_0fa3e4a5-1199-4abd-94ec-e40b406ba04f.json": b'{"turn": 1}',
    }
    status = dataset.completeness(files, {"completed_steps": ["share"]})
    assert "reasoning:share" in status["present"] and status["model_turns"] == 1
    row = dataset.run_row("run", files)
    assert row["model_turns"] == [{"turn": 1}] and row["steps"] == {}


def test_a_complete_run_reports_every_expected_record() -> None:
    status = dataset.completeness(_files(), RUN_STATE)
    assert status["complete"] is True
    assert status["missing"] == []
    assert "reasoning:scan" in status["present"] and "reasoning:share" in status["present"]
    assert status["model_turns"] == 1


def test_a_run_from_before_the_records_is_reported_incomplete() -> None:
    files = _files()
    del files["calls.json"], files["policy.json"], files["transcripts/scan/model/01-turns.jsonl"]
    status = dataset.completeness(files, RUN_STATE)
    assert status["complete"] is False
    assert status["missing"] == ["calls.json", "policy.json"]
    # scan still has usage.json under transcripts, which counts as evidence.
    assert "reasoning:scan" in status["present"]


def test_run_row_groups_steps_transcripts_turns_and_attachments() -> None:
    row = dataset.run_row("run-1", _files(), workflow={"workflow_id": "wf", "name": "x"})
    assert row["schema_version"] == "outcomeci.dataset.run/v1"
    assert row["trigger"]["type"] == "cron"
    assert row["steps"]["scan"]["outputs"] == {"posts": []}
    assert row["steps"]["share"]["items"][0]["value"]["priority"] == 1
    assert row["transcripts"]["scan"]["model/01-turns.jsonl"] == [{"turn": 1}, {"turn": 2}]
    assert row["model_turns"] == [{"step": "share", "status": "succeeded"}]
    attachment = row["attachments"][0]["value"]
    assert attachment["bytes"] == 16 and len(attachment["sha256"]) == 64
    assert row["completeness"]["complete"] is True


def test_local_export_walks_outcome_directories(tmp_path: Path) -> None:
    for run_id in ("20261007-a", "20261007-b"):
        directory = tmp_path / ".outcomeci" / "outcomes" / run_id
        for path, content in _files().items():
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    (tmp_path / ".outcomeci" / "outcomes" / "20261007-b" / "calls.json").unlink()
    rows: list[dict] = []
    report = dataset.local_export(tmp_path, emit=rows.append)
    assert [run["run_id"] for run in report["runs"]] == ["20261007-a", "20261007-b"]
    assert report["complete"] == 1 and report["incomplete"] == 1
    assert report["runs"][1]["missing"] == ["calls.json"]
    assert [row["run_id"] for row in rows] == ["20261007-a", "20261007-b"]


def test_workspace_export_lists_downloads_and_reports(monkeypatch) -> None:
    files = _files()
    listing = {
        "runs/": {
            "folders": [{"name": "run-1", "path": "runs/run-1/"}],
            "files": [],
            "cursor": None,
        },
        "runs/run-1/": {
            "folders": [
                {"name": "scan", "path": "runs/run-1/scan/"},
                {"name": "transcripts", "path": "runs/run-1/transcripts/"},
            ],
            "files": [
                {"path": f"runs/run-1/{name}", "object_id": name, "version_id": "v1"}
                for name in (
                    "run.json",
                    "manifest.json",
                    "effects.json",
                    "calls.json",
                    "policy.json",
                )
            ],
            "cursor": None,
        },
        "runs/run-1/scan/": {
            "folders": [],
            "files": [
                {
                    "path": "runs/run-1/scan/outputs.json",
                    "object_id": "scan/outputs.json",
                    "version_id": "v1",
                }
            ],
            "cursor": None,
        },
        "runs/run-1/transcripts/": {
            "folders": [{"name": "scan", "path": "runs/run-1/transcripts/scan/"}],
            "files": [],
            "cursor": None,
        },
        "runs/run-1/transcripts/scan/": {
            "folders": [],
            "files": [
                {
                    "path": "runs/run-1/transcripts/scan/usage.json",
                    "object_id": "transcripts/scan/usage.json",
                    "version_id": "v1",
                }
            ],
            "cursor": None,
        },
    }
    monkeypatch.setattr(cloud, "storage_directory", lambda ws, path="", cursor=None: listing[path])
    monkeypatch.setattr(cloud, "download_object", lambda ws, entry: files[entry["object_id"]])
    monkeypatch.setattr(
        cloud, "list_workflows", lambda ws: [{"workflow_id": "wf", "name": "x", "revision": 3}]
    )
    monkeypatch.setattr(
        cloud,
        "list_workflow_runs",
        lambda ws, wf: [{"run_id": "run-1", "status": "completed", "trigger_type": "cron"}],
    )
    rows: list[dict] = []
    report = dataset.workspace_export("ws", emit=rows.append)
    assert report["complete"] == 0 and report["incomplete"] == 1
    # share's outputs and reasoning are not stored, and the check names them.
    assert report["runs"][0]["missing"] == ["share/outputs.json", "reasoning:share"]
    assert rows[0]["workflow"] == {"workflow_id": "wf", "name": "x"}
    assert rows[0]["run"]["trigger_type"] == "cron"
    assert rows[0]["steps"]["scan"]["outputs"] == {"posts": []}

    check = dataset.workspace_export("ws", check_only=True)
    assert check["runs"][0]["missing"] == ["share/outputs.json", "reasoning:share"]
