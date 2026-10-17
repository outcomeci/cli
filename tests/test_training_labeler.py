"""The training labeler: a workflow/v1 run's annotation, written once it completes."""

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
    "confidence": 0.85,
    "labels": ["fix_opened", "announced"],
    "explanation": "The fix step opened a PR and announce posted it.",
    "evidence": [{"step": "fix", "description": "PR opened"}],
}


def _declare_labeler(path: Path, key: bool = True, **extra) -> None:
    document = yaml.safe_load(path.read_text())
    document["apiVersion"] = "outcomeci.workflow/v1"
    document["secrets"]["openai"] = "vault:openai/api-key"
    document["training"] = {
        "labeler": {
            "model": "openai/gpt-4.1-mini",
            **({"key": "secrets.openai"} if key else {}),
            **extra,
        }
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def _run(
    workflow: Path, script: dict, monkeypatch, events: list | None = None, decision_client=None
):
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
    if events is None:
        result = model_steps._sentry_run(workflow, model, sentry.Agent(), monkeypatch)
        return result, model
    from outcomeci.runtime import container as run_container
    from outcomeci.runtime import engine as local
    from outcomeci.workflow.compiler import compile_workflow

    monkeypatch.setattr(local, "invoke", sentry.Agent())
    config = workflow / sentry.WORKFLOW
    options = local.ExecutionOptions(
        credential_resolver=lambda reference: "xoxb-or-ghp-token",
        policy_reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
        model_client=model,
        decision_client=decision_client,
        event_sink=events.append,
    )
    result = run_container.execute(
        workflow,
        config,
        compile_workflow(config),
        "webhook",
        sentry._payload(),
        options,
        auto_continue=True,
    )
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
    assert result["training"]["labeler"]["confidence"] == 0.85
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
    assert annotation["confidence"] == 0.85
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
                "apiVersion": "outcomeci.workflow/v1",
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


def test_a_result_without_a_confidence_goes_back_to_the_labeler(
    model_steps_sentry_workflow, monkeypatch
):
    workflow = model_steps_sentry_workflow
    _declare_labeler(workflow / sentry.WORKFLOW)
    without = {key: value for key, value in LABELS.items() if key != "confidence"}
    result, model = _run(
        workflow,
        {
            models.LABELER_PROFILE: [
                {"calls": [model_steps._call("return_result", without, 1)]},
                {"calls": [model_steps._call("return_result", {**LABELS, "confidence": 0.3}, 2)]},
            ]
        },
        monkeypatch,
    )
    assert result["training"]["labeler"]["status"] == "completed"
    assert result["training"]["labeler"]["confidence"] == 0.3
    # The first result was refused for its missing confidence, not accepted.
    refused = json.loads(model.calls[-1]["messages"][-1]["content"])
    assert "confidence" in refused["error"]


def _labeled(confidence: float) -> dict:
    return {models.LABELER_PROFILE: [{"calls": [_result({**LABELS, "confidence": confidence})]}]}


def _result(labels: dict) -> dict:
    return model_steps._call("return_result", labels)


@pytest.mark.parametrize(("confidence", "required"), [(0.3, True), (0.6, False), (0.9, False)])
def test_review_below_routes_a_low_confidence_annotation_to_a_person(
    model_steps_sentry_workflow, monkeypatch, confidence, required
):
    workflow = model_steps_sentry_workflow
    _declare_labeler(workflow / sentry.WORKFLOW, review_below=0.6)
    events: list = []
    result, _model = _run(workflow, _labeled(confidence), monkeypatch, events)

    review = {"required": required, "threshold": 0.6, "confidence": confidence}
    assert result["training"]["labeler"]["review"] == review
    run_dir = workflow / ".outcomeci/outcomes" / result["run_id"]
    assert json.loads((run_dir / "training/annotation.json").read_text())["review"] == review
    routed = [item for item in events if item["event_type"] == "training.review_required"]
    assert len(routed) == (1 if required else 0)
    if required:
        assert routed[0]["level"] == "warning" and "0.6" in routed[0]["reason"]


def test_without_review_below_nothing_is_routed(model_steps_sentry_workflow, monkeypatch):
    workflow = model_steps_sentry_workflow
    _declare_labeler(workflow / sentry.WORKFLOW)
    events: list = []
    result, _model = _run(workflow, _labeled(0.1), monkeypatch, events)
    assert "review" not in result["training"]["labeler"]
    assert not [item for item in events if item["event_type"].startswith("training.")]


def test_the_labeler_sees_the_workflow_objective(model_steps_sentry_workflow, monkeypatch):
    workflow = model_steps_sentry_workflow
    _declare_labeler(workflow / sentry.WORKFLOW)
    document = yaml.safe_load((workflow / sentry.WORKFLOW).read_text())
    document["objective"] = "Fix the reported error for good."
    (workflow / sentry.WORKFLOW).write_text(yaml.safe_dump(document, sort_keys=False))
    _result, model = _run(workflow, _labeled(0.8), monkeypatch)
    record = json.loads(model.calls[-1]["messages"][1]["content"].split("\n")[-1])
    assert record["objective"] == "Fix the reported error for good."
    assert "objective" in model.calls[-1]["messages"][1]["content"]


def test_completed_workflow_invokes_optional_decision_labeler(
    model_steps_sentry_workflow, monkeypatch
):
    from unittest.mock import Mock

    from test_decision_labeler import response

    workflow = model_steps_sentry_workflow
    _declare_labeler(
        workflow / sentry.WORKFLOW, mode="decision", model="openai/gpt-6-luna", review_below=0.6
    )
    decide = Mock(return_value=response(outcome="success", category="none", confidence=0.9))
    result, _ = _run(workflow, {}, monkeypatch, events=[], decision_client=decide)
    assert result["status"] == "completed"
    assert result["training"]["labeler"]["status"] == "completed"
    assert result["training"]["labeler"]["outcome"] == "success"
    decide.assert_called_once()
    assert decide.call_args.kwargs["step"] == models.LABELER_PROFILE
