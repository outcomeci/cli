from __future__ import annotations

from pathlib import Path

from outcomeci.manifest import build_manifest


def test_manifest_has_one_backend_independent_shape(tmp_path: Path) -> None:
    outcome = tmp_path / ".outcomeci" / "outcomes" / "run-1"
    outcome.mkdir(parents=True)
    (outcome / "standup.md").write_text("standup\n")
    (outcome / ".broker").mkdir()
    (outcome / ".broker" / "journal.json").write_text(
        '{"references":{"izzy":"private-provider-id"}}'
    )
    common = {
        "outcome_root": outcome,
        "artifact_base": tmp_path,
        "run_id": "run-1",
        "phase": "intake",
        "workflow_revision": "workflow-revision",
        "context_revision_id": "context-revision",
        "constitution_sha256": "constitution-sha",
        "repository_base_commits": {"org/repo": "commit-sha"},
        "runner": "codex",
        "model": None,
        "transcript": {"files": [], "usage_records": 0},
    }
    local = build_manifest(
        **common,
        workflow_run_id=None,
        trajectory_version=None,
        backend_provider="filesystem",
        state_repository=None,
        context_provider="filesystem",
    )
    managed = build_manifest(
        **common,
        workflow_run_id="workflow-run-1",
        trajectory_version=2,
        backend_provider="outcomeci",
        state_repository="org/state",
        context_provider="outcomeci",
    )
    assert local.keys() == managed.keys()
    assert local["runner"].keys() == managed["runner"].keys()
    assert local["backend"].keys() == managed["backend"].keys()
    assert local["context"].keys() == managed["context"].keys()
    assert local["phase_contract"] is None
    assert all(".broker" not in path for path in local["artifacts"])
