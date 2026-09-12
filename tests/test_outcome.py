from pathlib import Path

import pytest

from outcomeci import outcome
from outcomeci.outcome import _claim, _expected, _transcripts, _validate_claim_phase, _validate_trajectory
from outcomeci.process import ExecutionError


def test_claim_accepts_managed_plan(tmp_path: Path) -> None:
    path = tmp_path / "claim.json"
    path.write_text('{"outcome_run_id":"r","workflow_run_id":"w","phase":"plan","trajectory_version":1,"agent":"codex","model":null,"state_repository":"org/state","targets":[{"repository":"org/repo"}],"intent_context":{}}')
    assert _claim(path)["phase"] == "plan"


def test_claim_accepts_custom_phase_for_workflow_validation(tmp_path: Path) -> None:
    path = tmp_path / "claim.json"
    path.write_text('{"outcome_run_id":"r","workflow_run_id":"w","phase":"deploy","trajectory_version":1,"agent":"codex","model":null,"state_repository":"org/state","targets":[{"repository":"org/repo"}],"intent_context":{}}')
    assert _claim(path)["phase"] == "deploy"


def test_plan_requires_standup_and_repo_artifacts(tmp_path: Path) -> None:
    values = _expected(tmp_path, ["outcomeci/www"], "plan")
    assert tmp_path / "standup.md" in values
    assert tmp_path / "specs/outcomeci-www/spec.md" in values
    assert tmp_path / "plans/outcomeci-www/plan.md" in values


def test_responsibility_extends_stable_candidate_role() -> None:
    claim = {"intent_context": {"ontology_revision_id": "rev-1"}}
    value = {"schema_version": "1", "ontology_revision_id": "rev-1", "targets": [{"repository_id": "R1", "repository": "outcomeci/api", "rationale": "match", "candidates": [{"role": "domain_logic", "responsibility": "usage_source", "disposition": "inspect"}]}]}
    assert _validate_trajectory(value, claim)["targets"][0]["candidates"][0]["responsibility"] == "usage_source"


def test_arbitrary_value_cannot_replace_stable_role() -> None:
    claim = {"intent_context": {"ontology_revision_id": "rev-1"}}
    value = {"schema_version": "1", "ontology_revision_id": "rev-1", "targets": [{"repository_id": "R1", "repository": "outcomeci/api", "rationale": "match", "candidates": [{"role": "usage_source", "disposition": "inspect"}]}]}
    with pytest.raises(ExecutionError, match="stable contract"):
        _validate_trajectory(value, claim)


def test_interactive_transcript_captures_only_bytes_after_begin(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "rollout-session-1.jsonl"
    prefix = '{"type":"session_meta","payload":{"id":"session-1","cwd":"/repo"}}\n'
    session.write_text(prefix)
    offset = session.stat().st_size
    session.write_text(prefix + '{"usage":{"input_tokens":12,"output_tokens":4}}\n')
    monkeypatch.setattr(outcome, "_sessions", lambda agent: [session])

    captured = _transcripts(
        "codex", tmp_path / "outcome", "intake",
        session_id="session-1", byte_offset=offset, workspace=Path("/repo"),
    )

    assert captured["files"][0]["source_offset"] == offset
    copied = tmp_path / "outcome" / captured["files"][0]["path"]
    assert "session_meta" not in copied.read_text()
    assert captured["usage_records"] == 1
    assert captured["usage"][0]["input_tokens"] == 12


def test_managed_claim_phase_must_have_completed_dependencies(tmp_path: Path) -> None:
    compiled = {"instructions": {"phases": {
        "intake": {"needs": [], "expects": {"outputs": [{"name": "packet", "path": "intake/packet.json", "required": True}]}},
        "plan": {"needs": ["intake"], "expects": {"outputs": []}},
    }}}
    with pytest.raises(ExecutionError, match="blocked by incomplete dependency intake"):
        _validate_claim_phase(compiled, tmp_path, "plan")
    packet = tmp_path / "intake" / "packet.json"
    packet.parent.mkdir()
    packet.write_text("{}")
    _validate_claim_phase(compiled, tmp_path, "plan")
