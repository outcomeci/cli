from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from outcomeci.config import ConfigError, compile_workflow

WORKFLOW = {
    "apiVersion": "outcomeci.workflow/v1",
    "name": "custom",
    "trigger": "manual",
    "reasoning": {"default": {"runner": "codex", "model": "gpt-default"}},
    "steps": [
        {
            "intake": {
                "reason": "intake.md",
                "from": "trigger",
                "returns": {"packet": {"intent": "string"}},
            }
        },
        {
            "review": {
                "reason": "review.md",
                "with": "intake.packet",
                "using": {"runner": "claude", "model": "claude-review"},
                "returns": {"review": "string"},
            }
        },
        {"plan": {"reason": "plan.md", "with": ["review.review"], "returns": {"plan": "string"}}},
    ],
}


def _workflow(tmp_path: Path, **changes) -> Path:
    instructions = tmp_path / ".outcomeci" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    for name in ("intake", "review", "plan"):
        (instructions / f"{name}.md").write_text(f"# {name}\n")
    value = {**WORKFLOW, **changes}
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return path


def test_compiles_a_v1_workflow(tmp_path: Path) -> None:
    compiled = compile_workflow(_workflow(tmp_path))
    assert compiled["api_version"] == "outcomeci.workflow/v1"
    assert compiled["engine_version"] == "2"


def test_a_v1alpha1_file_is_refused_with_the_version_to_use(tmp_path: Path) -> None:
    path = tmp_path / "outcome.yml"
    path.write_text("apiVersion: outcomeci.workflow/v1alpha1\nkind: OutcomeWorkflow\n")
    with pytest.raises(ConfigError, match="use outcomeci.workflow/v1"):
        compile_workflow(path)


def test_an_unknown_api_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unsupported apiVersion.*v9"):
        compile_workflow(_workflow(tmp_path, apiVersion="outcomeci.com/v9"))


def test_steps_run_in_order_with_their_own_agents(tmp_path: Path) -> None:
    compiled = compile_workflow(_workflow(tmp_path))
    assert compiled["graph"]["levels"] == [["intake"], ["review"], ["plan"]]
    assert compiled["triggers"] == {"manual": {"type": "manual"}}
    phases = compiled["instructions"]["phases"]
    assert phases["review"]["policy"] == {"runner": "claude", "model": "claude-review"}
    assert phases["plan"]["policy"] == {"runner": "codex", "model": "gpt-default"}
    assert phases["intake"]["expects"]["outputs"][0]["path"] == "intake/outputs.json"


def test_a_fallback_agent_reaches_the_compiled_workflow(tmp_path: Path) -> None:
    reasoning = {
        "default": {"runner": "codex"},
        "fallback": [{"runner": "claude", "model": "claude-opus-5"}],
    }
    compiled = compile_workflow(_workflow(tmp_path, reasoning=reasoning))
    assert compiled["workflow"]["spec"]["agents"]["default"]["fallback"] == {
        "runner": "claude",
        "model": "claude-opus-5",
    }


@pytest.mark.parametrize(
    "fallback,error",
    [
        ({"runner": "codex"}, "must differ from the default runner"),
        ({"runner": "not-a-runner"}, "runner must be one of"),
        ({}, "fallback\[0\].runner is required"),
    ],
)
def test_an_invalid_fallback_is_refused(tmp_path: Path, fallback: dict, error: str) -> None:
    reasoning = {"default": {"runner": "codex"}, "fallback": [fallback]}
    with pytest.raises(ConfigError, match=error):
        compile_workflow(_workflow(tmp_path, reasoning=reasoning))


def test_reasoning_accepts_only_runner_and_model(tmp_path: Path) -> None:
    reasoning = {"default": {"runner": "codex", "bogus": True}}
    with pytest.raises(ConfigError, match="supports runner and model"):
        compile_workflow(_workflow(tmp_path, reasoning=reasoning))


def test_a_trigger_is_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="trigger must be one of"):
        compile_workflow(_workflow(tmp_path, trigger=None))


def test_an_email_trigger_compiles(tmp_path: Path) -> None:
    compiled = compile_workflow(_workflow(tmp_path, trigger="email"))
    assert compiled["triggers"] == {"email": {"type": "email.received"}}


def test_a_cron_trigger_compiles_with_expression_and_timezone(tmp_path: Path) -> None:
    trigger = {"type": "cron", "expression": "*/15 * * * *", "timezone": "America/Chicago"}
    compiled = compile_workflow(_workflow(tmp_path, trigger=trigger))
    assert compiled["triggers"]["cron"] == trigger


@pytest.mark.parametrize(
    "trigger,error",
    [
        (
            {"type": "cron", "expression": "* * * * *", "timezone": "America/Chicago"},
            "more than once every five minutes",
        ),
        (
            {"type": "cron", "expression": "*/3 * * * *", "timezone": "America/Chicago"},
            "more than once every five minutes",
        ),
        (
            {"type": "cron", "expression": "0 9 5 * 2", "timezone": "America/Chicago"},
            "day-of-month or day-of-week",
        ),
        ({"type": "cron", "expression": "0 9 * * *"}, "timezone is required"),
        (
            {"type": "cron", "expression": "0 9 * * *", "timezone": "Not/AZone"},
            "not a recognized IANA time zone",
        ),
        (
            {"type": "cron", "expression": "0 9 * *", "timezone": "America/Chicago"},
            "five space-separated fields",
        ),
    ],
)
def test_an_invalid_cron_trigger_is_refused(tmp_path: Path, trigger: dict, error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        compile_workflow(_workflow(tmp_path, trigger=trigger))


def test_a_step_can_read_only_earlier_outputs(tmp_path: Path) -> None:
    steps = [dict(step) for step in WORKFLOW["steps"]]
    steps[2] = {"plan": {"reason": "plan.md", "with": ["review.missing"]}}
    with pytest.raises(ConfigError, match="returns no missing"):
        compile_workflow(_workflow(tmp_path, steps=steps))


def test_step_names_are_unique(tmp_path: Path) -> None:
    steps = [*WORKFLOW["steps"], {"intake": {"reason": "plan.md"}}]
    with pytest.raises(ConfigError, match="duplicate or reserved step name"):
        compile_workflow(_workflow(tmp_path, steps=steps))


def test_field_order_does_not_change_the_revision(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    first = compile_workflow(path)["workflow_revision"]
    value = yaml.safe_load(path.read_text())
    value = dict(reversed(list(value.items())))
    value["steps"] = [
        {name: dict(reversed(list(step.items())))}
        for entry in value["steps"]
        for name, step in entry.items()
    ]
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    assert compile_workflow(path)["workflow_revision"] == first
