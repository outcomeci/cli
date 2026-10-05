"""Approval belongs to the discussion result, never the plan's content."""

import copy

import pytest

from outcomeci import v1_runtime


@pytest.mark.parametrize(
    ("status", "decision"), [("converged", "approved"), ("rejected", "rejected")]
)
def test_explicit_decision_closes_unchanged_plan(monkeypatch, status, decision):
    plan = {"summary": "Add Linear", "files": ["linear.py"]}
    conversation = {
        "plan": plan,
        "current_version": 1,
        "versions": [{"version": 1, "plan": copy.deepcopy(plan)}],
        "turns": [{"from": "human", "message": decision}],
        "outbox": [],
    }
    monkeypatch.setattr(
        v1_runtime,
        "_turn",
        lambda *args: {"status": status, "plan": copy.deepcopy(plan), "message": decision},
    )
    v1_runtime._answer(None, None, None, None, None, conversation, None, None, None)
    assert conversation["decision"] == decision
    assert conversation["closing"] == status
    assert conversation["plan"] == plan
    assert conversation["current_version"] == 1
    assert conversation["outbox"] == [decision]


@pytest.mark.parametrize("field", ["summary", "decision"])
def test_changed_plan_never_inherits_approval_even_for_a_field_named_decision(monkeypatch, field):
    # A legacy workflow may place decision inside a plan. Do not silently ignore
    # arbitrary plan fields; migrate that workflow to a separate result instead.
    plan = {"summary": "Add Linear", "decision": "pending"}
    answer = {**plan, field: "approved"}
    conversation = {
        "plan": plan,
        "current_version": 1,
        "versions": [{"version": 1, "plan": copy.deepcopy(plan)}],
        "turns": [{"from": "human", "message": "approved"}],
        "outbox": [],
    }
    monkeypatch.setattr(
        v1_runtime,
        "_turn",
        lambda *args: {"status": "converged", "plan": answer, "message": "Approved."},
    )
    v1_runtime._answer(None, None, None, None, None, conversation, None, None, None)
    assert conversation["decision"] == "undecided"
    assert "closing" not in conversation
    assert conversation["current_version"] == 2
    assert conversation["versions"][-1]["diff"]["changed"] == [field]
    assert "Please review" in conversation["outbox"][0]
    assert conversation["turns"][-1]["message"] != "Approved."


@pytest.mark.parametrize(
    ("status", "expected", "pull_count"),
    [("converged", "approved", 2), ("rejected", "rejected", 0)],
)
def test_decision_output_controls_downstream_writes(
    tmp_path, monkeypatch, status, expected, pull_count
):
    import json

    import test_v1_slack as example
    from test_v1_runtime import _slack_root

    root = _slack_root(tmp_path, monkeypatch)
    slack = example.Slack(["approved" if status == "converged" else "rejected"])

    def turn(root, compiled, state, step, spec, conversation, runner, model, options):
        return {"status": status, "plan": copy.deepcopy(conversation["plan"]), "message": expected}

    monkeypatch.setattr(v1_runtime, "_turn", turn)
    result = example._run(root, slack, example.Agent(), monkeypatch)
    outputs = json.loads(
        (root / ".outcomeci/outcomes" / result["run_id"] / "discuss/outputs.json").read_text()
    )
    assert outputs["decision"] == expected
    assert outputs["status"] == status
    assert "decision" not in outputs["plan"]
    assert len(slack.pulls) == pull_count
    assert result["status"] == "completed"
