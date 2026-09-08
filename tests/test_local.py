from __future__ import annotations

import json
import re
from pathlib import Path

from outcomeci import local
from outcomeci.repository import initialize


def _fake_invoke(agent: str, model: str | None, prompt: str, workspace: Path, timeout: int) -> str:
    match = re.search(r"beneath (.+?)\. During", prompt)
    assert match
    root = Path(match.group(1))
    root.mkdir(parents=True, exist_ok=True)
    (root / "standup.md").write_text("# Standup: local\n**Status**: active\n")
    if "# Intake phase" in prompt:
        revision = re.search(r'ontology_revision_id\s+\\?"([^"\\]+)', prompt)
        assert revision
        target = root / "intake" / "trajectory.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({
            "schema_version": "1",
            "ontology_revision_id": revision.group(1),
            "targets": [{"repository_id": f"local:{workspace.name}", "repository": workspace.name, "rationale": "local repository", "candidates": []}],
        }))
    elif "# Plan phase" in prompt:
        for path in (root / "specs" / workspace.name / "spec.md", root / "plans" / workspace.name / "plan.md"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("complete\n")
    elif "# Tasks phase" in prompt:
        for path in (root / "tasks" / "repositories" / f"{workspace.name}.md", root / "tasks" / "tasks.md"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("complete\n")
    return "phase complete"


def test_local_start_and_continue_through_tasks(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    monkeypatch.setattr(local, "invoke", _fake_invoke)
    monkeypatch.setattr(local, "_transcripts", lambda *args: {"usage_records": 0, "files": [], "usage": []})

    state = local.start(tmp_path, tmp_path / "outcome.yml", "Improve local onboarding")
    assert state["status"] == "awaiting_confirmation"
    assert state["phase"] == "intake"
    manifest_path = tmp_path / ".outcomeci" / "outcomes" / state["run_id"] / "manifest.json"
    intake_manifest = json.loads(manifest_path.read_text())
    assert intake_manifest["schema_version"] == "outcomeci.outcome-manifest/v1alpha1"
    assert intake_manifest["backend"] == {"provider": "filesystem", "state_repository": None}
    assert intake_manifest["workflow_run_id"] is None
    assert intake_manifest["trajectory_version"] is None
    assert intake_manifest["standup"].endswith("/standup.md")
    assert "run.json" not in intake_manifest["artifacts"]
    state = local.continue_run(tmp_path, tmp_path / "outcome.yml", state["run_id"], True)
    assert state["phase"] == "plan"
    state = local.continue_run(tmp_path, tmp_path / "outcome.yml", state["run_id"], True)
    assert state["status"] == "ready_for_implementation"
    assert state["phase"] == "tasks"
    assert manifest_path.is_file()


def test_interactive_session_lifecycle(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    state = local.begin(tmp_path, tmp_path / "outcome.yml", "Improve local onboarding")
    context = local.compile_context(tmp_path, tmp_path / "outcome.yml", state["run_id"])
    assert context["run"]["status"] == "awaiting_agent"
    assert context["instructions"]["phase"]["path"].endswith("intake.md")

    prompt = (
        context["instructions"]["phase"]["content"]
        + f'\nontology_revision_id "{context["phase_contract"]["ontology_revision_id"]}"'
        + f"\nWrite beneath {context['outcome_root']}. During this test."
    )
    _fake_invoke("codex", None, prompt, tmp_path, 1)
    validated = local.validate_artifacts(tmp_path, tmp_path / "outcome.yml", state["run_id"])
    assert validated["status"] == "awaiting_confirmation"

    advanced = local.advance(tmp_path, tmp_path / "outcome.yml", state["run_id"], True)
    assert advanced["phase"] == "plan"
    assert advanced["status"] == "awaiting_agent"
