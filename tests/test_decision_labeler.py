"""Decision labeling uses the pinned rubric and keeps provider evidence separate."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from outcomeci.reasoning import decisions, models
from outcomeci.reasoning.labeling import decision_questions
from outcomeci.runtime import labeler
from outcomeci.runtime.engine import ExecutionOptions
from outcomeci.workflow.compiler import compile_workflow


def response(outcome="partial", category="missing_output", confidence=0.4):
    selected = {"outcome": outcome, "failure_category": category}
    return {
        "model": "decision-model",
        "answers": [
            {
                "name": name,
                "type": "choice",
                "choice": selected[name],
                "confidence": confidence if name == "outcome" else 0.9,
                "probabilities": [
                    {
                        "value": option["value"],
                        "probability": 1.0 if option["value"] == selected[name] else 0.0,
                    }
                    for option in question["choices"]
                ],
            }
            for name, question in decision_questions().items()
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 0,
            "input_tokens_details": {"cached_tokens": 25},
        },
    }


def compiled(tmp_path, model="typesafe/jev-latest"):
    path = tmp_path / "workflow.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v1",
                "trigger": "manual",
                "objective": "Deliver a verified fix.",
                "secrets": {"judge": "vault:models/judge"},
                "training": {
                    "labeler": {
                        "mode": "decision",
                        "model": model,
                        "key": "secrets.judge",
                        "review_below": 0.6,
                    }
                },
                "steps": [{"work": {"reason": "Fix it."}}],
            }
        )
    )
    return compile_workflow(path)


@pytest.mark.parametrize("confidence,review", [(0.4, True), (0.6, False), (0.9, False)])
def test_decision_labeler_retains_probabilities_and_routes_review(tmp_path, confidence, review):
    config = compiled(tmp_path)
    state = {"run_id": "test-run", "status": "completed", "steps": {"work": "completed"}}
    root = tmp_path / ".outcomeci/outcomes/test-run"
    (root / "work").mkdir(parents=True)
    (root / "work/outputs.json").write_text('{"fix": "proposed"}')
    decide = Mock(return_value=response(confidence=confidence))
    chat = Mock(side_effect=AssertionError("must not use chat"))
    events = []
    labeler.label_run(
        tmp_path,
        config,
        state,
        ExecutionOptions(decision_client=decide, model_client=chat, event_sink=events.append),
    )
    entry = state["training"]["labeler"]
    assert state["status"] == "completed" and entry["status"] == "completed"
    assert entry["mode"] == "decision" and entry["usage_records"] == 1
    annotation = json.loads((root / labeler.ANNOTATION).read_text())
    assert annotation["outcome"] == "partial"
    assert annotation["failure_category"] == "missing_output"
    assert annotation["confidence"] == confidence
    assert annotation["review"]["required"] is review
    assert annotation["explanation"] == "" and annotation["evidence"] == []
    assert (
        annotation["decisions"]["outcome"]["probabilities"]
        == response()["answers"][0]["probabilities"]
    )
    assert bool(events) is review
    payload = decide.call_args.kwargs
    assert payload["step"] == models.LABELER_PROFILE
    assert payload["input"]["objective"] == "Deliver a verified fix."
    assert payload["input"]["steps"][0]["outputs"] == {"fix": "proposed"}
    assert "vault:" not in json.dumps(payload)
    transcript = json.loads((root / annotation["transcript"]).read_text())
    assert transcript["usage"]["cache_read_tokens"] == 25
    chat.assert_not_called()


@pytest.mark.parametrize("failure", ["provider", "refusal", "malformed", "inconsistent", "usage"])
def test_bad_decision_annotations_never_change_completed_run(tmp_path, failure):
    config = compiled(tmp_path)
    state = {"run_id": "test-run", "status": "completed"}
    result = response()
    if failure == "refusal":
        result["answers"][0] = {"name": "outcome", "type": "refusal"}
    elif failure == "malformed":
        result["answers"][0]["probabilities"][0]["probability"] = 4
    elif failure == "inconsistent":
        result = response(outcome="success", category="missing_output")
    elif failure == "usage":
        result["usage"]["input_tokens"] = -1
    decide = Mock(
        return_value=result,
        side_effect=RuntimeError("provider unavailable") if failure == "provider" else None,
    )
    labeler.label_run(tmp_path, config, state, ExecutionOptions(decision_client=decide))
    assert state["status"] == "completed"
    assert state["training"]["labeler"]["status"] == "failed"
    assert not (tmp_path / ".outcomeci/outcomes/test-run" / labeler.ANNOTATION).exists()
    assert (
        tmp_path / ".outcomeci/outcomes/test-run/transcripts/training.labeler/model/01-turns.jsonl"
    ).exists()


@pytest.mark.parametrize("model", ["openai/gpt-6-luna", "typesafe/jev-latest"])
def test_local_labeler_resolves_its_own_key_and_fixed_rubric(tmp_path, monkeypatch, model):
    import litellm

    from outcomeci.reasoning import decision_transport
    from outcomeci.runtime import launcher

    config = compiled(tmp_path, model)
    resolve = Mock(return_value="private-test-key")
    provider = Mock(return_value=SimpleNamespace(model_dump=lambda **kwargs: response()))
    monkeypatch.setattr(litellm, "decisions", provider)
    monkeypatch.setattr(decision_transport, "install_decision_response_guard", lambda *a, **k: None)
    result = decisions.local_client(config, resolve)(
        step=models.LABELER_PROFILE, input={"objective": "Fix it"}
    )
    assert result == response()
    resolve.assert_called_once_with("vault:models/judge")
    assert launcher._vault_references(config) == ["vault:models/judge"]
    kwargs = provider.call_args.kwargs
    assert kwargs["api_key"] == "private-test-key" and kwargs["model"] == model
    assert kwargs["questions"] == [
        {"name": name, **question} for name, question in decision_questions().items()
    ]
    assert kwargs["api_base"] == (
        "https://api.typesafe.ai" if model.startswith("typesafe/") else "https://api.openai.com/v1"
    )
    assert kwargs["num_retries"] == 0 and kwargs["no-log"] is True
