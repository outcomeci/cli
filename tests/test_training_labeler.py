"""The training labeler: a workflow/v2 run's annotation, written once it completes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_model_steps as model_steps
import test_v1_sentry as sentry
import yaml

from outcomeci.reasoning import models

LABELS = {
    "outcome": "success",
    "labels": ["fix_opened", "announced"],
    "explanation": "The fix step opened a PR and announce posted it.",
    "evidence": [{"step": "fix", "description": "PR opened"}],
}


def _declare_labeler(path: Path, key: bool = True) -> None:
    document = yaml.safe_load(path.read_text())
    document["apiVersion"] = "outcomeci.workflow/v2"
    document["secrets"]["openai"] = "vault:openai/api-key"
    document["training"] = {
        "labeler": {"model": "openai/gpt-4.1-mini", **({"key": "secrets.openai"} if key else {})}
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def _run(workflow: Path, script: dict, monkeypatch):
    model_steps._profiles(workflow / sentry.WORKFLOW, {"triage": "light", "announce": "light"})
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    model = model_steps.Model(
        {
            "triage": [
                {"calls": [model_steps._call("slack__post", {"text": "*Sentry alert*"}, 1)]},
                {"calls": [model_steps._call("return_result", model_steps.TRIAGE, 2)]},
            ],
            "announce": [{"text": "Posted."}],
            **script,
        }
    )
    result = model_steps._sentry_run(workflow, model, sentry.Agent(), monkeypatch)
    return result, model


def test_a_declared_labeler_annotates_the_completed_run(model_steps_sentry_workflow, monkeypatch):
    workflow = model_steps_sentry_workflow
    _declare_labeler(workflow / sentry.WORKFLOW)
    result, model = _run(
        workflow,
        {
            models.LABELER_PROFILE: [
                {
                    "calls": [model_steps._call("return_result", LABELS)],
                    "usage": {"input_tokens": 900, "output_tokens": 60},
                }
            ]
        },
        monkeypatch,
    )

    assert result["status"] == "completed"
    assert result["training"]["labeler"]["status"] == "completed"
    assert result["training"]["labeler"]["outcome"] == "success"
    # The labeler ran last, with its reserved profile, and saw every step's result.
    assert model.calls[-1]["profile"] == models.LABELER_PROFILE
    assert [call["step"] for call in model.calls][-1] == models.LABELER_PROFILE
    record = json.loads(model.calls[-1]["messages"][1]["content"].split("\n")[-1])
    assert record["status"] == "completed"
    names = [step["name"] for step in record["steps"]]
    assert names[0] == "triage" and {"fix", "announce"} <= set(names)
    assert all(step["state"] == "completed" for step in record["steps"])
    assert record["steps"][0]["outputs"]["decision"] == "fix"
    run_dir = workflow / ".outcomeci/outcomes" / result["run_id"]
    annotation = json.loads((run_dir / "training/annotation.json").read_text())
    assert annotation["outcome"] == "success" and annotation["labels"] == LABELS["labels"]
    assert annotation["labeler"] == {
        "model": "openai/gpt-4.1-mini",
        "profile": models.LABELER_PROFILE,
        "rubric_version": 1,
    }
    assert annotation["run_id"] == result["run_id"] and annotation["created_at"]
    usage = json.loads((run_dir / f"transcripts/{models.LABELER_PROFILE}/usage.json").read_text())
    assert usage["provider"] == "openai" and usage["records"][0]["input_tokens"] == 900
    # Written on disk too, so the record a runner uploads carries it.
    stored = json.loads((run_dir / "run.json").read_text())
    assert stored["training"]["labeler"]["path"] == "training/annotation.json"


def test_a_failing_labeler_does_not_fail_the_run(model_steps_sentry_workflow, monkeypatch):
    workflow = model_steps_sentry_workflow
    _declare_labeler(workflow / sentry.WORKFLOW)
    result, _model = _run(
        workflow,
        # Never calls return_result: the labeler's own error, not the run's.
        {models.LABELER_PROFILE: [{"text": "looks fine"}, {"text": "still fine"}]},
        monkeypatch,
    )

    assert result["status"] == "completed" and "error" not in result
    entry = result["training"]["labeler"]
    assert entry["status"] == "failed" and "return_result" in entry["error"]
    run_dir = workflow / ".outcomeci/outcomes" / result["run_id"]
    assert not (run_dir / "training/annotation.json").exists()
    # Its transcript survives, so someone can see what it was asked.
    assert (run_dir / f"transcripts/{models.LABELER_PROFILE}/model/01-turns.jsonl").exists()


def test_a_run_without_a_labeler_is_not_annotated(model_steps_sentry_workflow, monkeypatch):
    workflow = model_steps_sentry_workflow
    result, model = _run(workflow, {}, monkeypatch)
    assert result["status"] == "completed" and "training" not in result
    assert models.LABELER_PROFILE not in [call["profile"] for call in model.calls]


def test_the_local_client_resolves_the_labeler_profile(tmp_path):
    compiled = {"reasoning": {}, "training": {"labeler": {"model": "openai/gpt-4.1-mini"}}}
    assert models.profile_spec(compiled, models.LABELER_PROFILE) == {"model": "openai/gpt-4.1-mini"}
    assert models.profile_spec({"reasoning": {}}, models.LABELER_PROFILE) is None
    assert models.profile_spec({"reasoning": {"light": {"model": "x"}}}, "light") == {"model": "x"}


@pytest.fixture
def model_steps_sentry_workflow(tmp_path, monkeypatch):
    return model_steps.sentry_workflow.__wrapped__(tmp_path, monkeypatch)


def test_a_local_run_leases_the_keys_its_model_profiles_and_labeler_name(tmp_path):
    from outcomeci.runtime import launcher
    from outcomeci.workflow.compiler import compile_workflow

    (tmp_path / "outcome.yml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v2",
                "name": "smoke",
                "trigger": "manual",
                "secrets": {"anthropic": "vault:models/anthropic", "openai": "vault:models/openai"},
                "reasoning": {
                    "light": {"model": "anthropic/claude-haiku-4-5", "key": "secrets.anthropic"}
                },
                "training": {"labeler": {"model": "openai/gpt-4.1-mini", "key": "secrets.openai"}},
                "steps": [{"work": {"reason": "Do it.", "using": "light"}}],
            },
            sort_keys=False,
        )
    )
    compiled = compile_workflow(tmp_path / "outcome.yml")
    assert launcher._vault_references(compiled) == [
        "vault:models/anthropic",
        "vault:models/openai",
    ]
