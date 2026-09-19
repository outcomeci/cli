from __future__ import annotations

import json
import re
from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from outcomeci import local
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize


@pytest.fixture(autouse=True)
def capability_context(monkeypatch):
    monkeypatch.setattr(local, "serve_capability", lambda *args, **kwargs: nullcontext({}))


def test_continue_run_forwards_the_cloud_execution_context(tmp_path: Path, monkeypatch) -> None:
    state = {
        "run_id": "run-1",
        "status": "awaiting_confirmation",
        "completed_phases": ["resolve_analytics"],
    }
    local._write(tmp_path, state)
    monkeypatch.setattr(
        local,
        "compile_workflow",
        lambda config: {
            "instructions": {
                "phases": {
                    "resolve_analytics": {"needs": []},
                    "notify": {"needs": ["resolve_analytics"]},
                }
            }
        },
    )
    captured = {}

    def fake_execute(root, config, state, **options):
        captured.update(options)
        return {"run_id": state["run_id"], "status": "completed"}

    monkeypatch.setattr(local, "_execute", fake_execute)
    resolver = lambda reference: "value"  # noqa: E731

    result = local.continue_run(
        tmp_path,
        tmp_path / "outcome.yml",
        "run-1",
        approve=True,
        credential_resolver=resolver,
        execution_backend="outcomeci",
        _container_isolated=True,
    )

    assert result["status"] == "completed"
    assert captured["credential_resolver"] is resolver
    assert captured["execution_backend"] == "outcomeci"
    assert captured["_container_isolated"] is True


def _fake_invoke(
    agent: str, model: str | None, prompt: str, workspace: Path, timeout: int, **kwargs
) -> str:
    match = re.search(r"beneath (.+?)\. During", prompt)
    assert match
    root = Path(match.group(1))
    root.mkdir(parents=True, exist_ok=True)
    (root / "standup.md").write_text("# Standup: local\n**Status**: active\n")
    if "# Intake phase" in prompt:
        revision = re.search(r'ontology_revision_id\s+\\?"([^"\\]+)', prompt)
        assert revision
        target = root / "intake" / "trajectory.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "ontology_revision_id": revision.group(1),
                    "targets": [
                        {
                            "repository_id": f"local:{workspace.name}",
                            "repository": workspace.name,
                            "rationale": "local repository",
                            "candidates": [],
                        }
                    ],
                }
            )
        )
    elif "# Plan phase" in prompt:
        for path in (
            root / "specs" / workspace.name / "spec.md",
            root / "plans" / workspace.name / "plan.md",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("complete\n")
    elif "# Tasks phase" in prompt:
        for path in (
            root / "tasks" / "repositories" / f"{workspace.name}.md",
            root / "tasks" / "tasks.md",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("complete\n")
    return "phase complete"


def test_local_start_and_continue_through_tasks(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    monkeypatch.setattr(local, "invoke", _fake_invoke)
    monkeypatch.setattr(
        local,
        "_transcripts",
        lambda *args, **kwargs: {"usage_records": 0, "files": [], "usage": []},
    )

    state = local.start(tmp_path, tmp_path / "outcome.yml", "Improve local onboarding")
    assert state["status"] == "awaiting_input"
    assert state["pending_interaction"]["id"] == "confirm_intent"
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
    assert validated["status"] == "awaiting_input"

    answered = local.respond(
        tmp_path,
        tmp_path / "outcome.yml",
        state["run_id"],
        "confirm_intent",
        "The scope is correct.",
        approve=True,
    )
    assert answered["status"] == "awaiting_confirmation"
    advanced = local.advance(tmp_path, tmp_path / "outcome.yml", state["run_id"], True)
    assert advanced["phase"] == "plan"
    assert advanced["status"] == "awaiting_agent"


def test_manual_execution_requires_manual_trigger(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    path = tmp_path / "outcome.yml"
    value = yaml.safe_load(path.read_text())
    value["spec"]["triggers"] = {"mail": {"type": "email.received"}}
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    with pytest.raises(ExecutionError, match="manual trigger"):
        local.begin(tmp_path, path, "This must arrive by email")


def test_ready_set_supports_parallel_phases_and_join(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    compiled = local.compile_workflow(tmp_path / "outcome.yml")
    phases = compiled["instructions"]["phases"]
    phases["product_review"] = {**phases["plan"], "needs": ["intake"]}
    phases["technical_review"] = {**phases["plan"], "needs": ["intake"]}
    phases["plan"]["needs"] = ["product_review", "technical_review"]
    assert local._ready(compiled, ["intake"]) == ["product_review", "technical_review"]
    assert local._ready(compiled, ["intake", "product_review"]) == ["technical_review"]
    assert local._ready(compiled, ["intake", "product_review", "technical_review"]) == ["plan"]


def test_declared_json_schema_is_enforced(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    compiled = local.compile_workflow(tmp_path / "outcome.yml")
    contract = compiled["instructions"]["phases"]["intake"]["expects"]["outputs"][0]
    contract["schema"] = ".outcomeci/schemas/test.json"
    compiled["instructions"]["schemas"][contract["schema"]] = {
        "value": {"type": "object", "required": ["intent"]}
    }
    outcome = tmp_path / ".outcomeci" / "outcomes" / "test"
    artifact = outcome / contract["path"]
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"wrong": true}')
    with pytest.raises(ExecutionError, match="failed JSON validation"):
        local._validate_outputs(compiled, outcome, "intake")


def test_launch_worker_records_detached_attempt(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    state = local.begin(tmp_path, tmp_path / "outcome.yml", "Durable outcome")

    class Process:
        pid = 4242

    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(local.subprocess, "Popen", popen)
    result = local.launch_worker(
        tmp_path,
        tmp_path / "outcome.yml",
        state["run_id"],
        "respond",
        interaction_id="confirm_intent",
        message="continue",
        approve=True,
    )
    worker = json.loads((Path(state["outcome_root"]) / "worker.json").read_text())
    assert result["status"] == "queued"
    assert worker["pid"] == 4242
    assert worker["status"] == "queued"
    assert calls[0][0][:3] == [local.sys.executable, "-m", "outcomeci.worker"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["close_fds"] is True


def test_recover_retries_only_stale_running_worker(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    state = local.begin(tmp_path, tmp_path / "outcome.yml", "Recover outcome")
    path = tmp_path / ".outcomeci" / "outcomes" / state["run_id"] / "run.json"
    current = json.loads(path.read_text())
    current["status"] = "running"
    path.write_text(json.dumps(current))
    monkeypatch.setattr(local, "_worker_live", lambda outcome_root: False)
    launched = {}

    def launch(root, config, run_id, operation, **kwargs):
        launched.update({"run_id": run_id, "operation": operation})
        return {"status": "queued"}

    monkeypatch.setattr(local, "launch_worker", launch)
    assert local.recover(tmp_path, tmp_path / "outcome.yml", state["run_id"])["status"] == "queued"
    assert launched == {"run_id": state["run_id"], "operation": "retry"}
    assert json.loads(path.read_text())["status"] == "error"


def test_before_interaction_is_durable_and_resumes_execution(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
    value = yaml.safe_load(workflow.read_text())
    intake = value["spec"]["agents"]["phases"]["intake"]
    intake.pop("integrations", None)
    intake["humans"] = {
        "before": [
            {
                "id": "confirm_direction",
                "participant": "requester",
                "purpose": "Stop a bad direction.",
                "interaction": "approval",
            }
        ],
        "during": [
            {
                "id": "ask_expert",
                "participant": "domain_expert",
                "purpose": "Resolve domain questions.",
                "interaction": "consultation",
                "availability": "on_demand",
            }
        ],
    }
    workflow.write_text(yaml.safe_dump(value, sort_keys=False))
    monkeypatch.setattr(local, "invoke", _fake_invoke)
    monkeypatch.setattr(
        local,
        "_transcripts",
        lambda *args, **kwargs: {"usage_records": 0, "files": [], "usage": []},
    )
    state = local.start(tmp_path, workflow, "Improve onboarding")
    assert state["status"] == "awaiting_input"
    request = Path(state["pending_interaction"]["path"])
    assert json.loads(request.read_text())["timing"] == "before"
    state = local.respond(
        tmp_path, workflow, state["run_id"], "confirm_direction", "Proceed.", approve=True
    )
    assert state["status"] == "awaiting_confirmation"
