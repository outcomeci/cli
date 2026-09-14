from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from outcomeci.config import ConfigError, compile_workflow


def _workflow(tmp_path: Path) -> Path:
    instructions = tmp_path / ".outcomeci" / "instructions"
    instructions.mkdir(parents=True)
    for name in ("orchestrate", "intake", "product", "technical", "plan"):
        (instructions / f"{name}.md").write_text(f"# {name}\n")
    schemas = tmp_path / ".outcomeci" / "schemas"
    schemas.mkdir()
    (schemas / "packet.json").write_text(json.dumps({"type": "object", "required": ["intent"]}))
    value = {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "custom"},
        "spec": {
            "triggers": {"manual": {"type": "manual"}},
            "backend": {"provider": "filesystem"},
            "context": {"provider": "filesystem"},
            "instructions": {
                "orchestrate": {
                    "path": ".outcomeci/instructions/orchestrate.md",
                    "model": "gpt-orchestrator",
                }
            },
            "agents": {
                "default": {"runner": "codex", "model": "gpt-default"},
                "phases": {
                    "intake": {
                        "instructions": ".outcomeci/instructions/intake.md",
                        "needs": [],
                        "expects": {
                            "inputs": [
                                {
                                    "name": "request",
                                    "from": "runtime.intent",
                                    "media_type": "text/plain",
                                }
                            ],
                            "outputs": [
                                {
                                    "name": "packet",
                                    "path": "intake/packet.json",
                                    "media_type": "application/json",
                                    "schema": ".outcomeci/schemas/packet.json",
                                }
                            ],
                        },
                    },
                    "product": {
                        "instructions": ".outcomeci/instructions/product.md",
                        "needs": ["intake"],
                        "runner": "claude",
                        "model": "claude-review",
                        "expects": {
                            "inputs": [
                                {
                                    "name": "packet",
                                    "from": "intake.outputs.packet",
                                    "media_type": "application/json",
                                }
                            ],
                            "outputs": [
                                {
                                    "name": "review",
                                    "path": "reviews/product.md",
                                    "media_type": "text/markdown",
                                }
                            ],
                        },
                    },
                    "technical": {
                        "instructions": ".outcomeci/instructions/technical.md",
                        "needs": ["intake"],
                        "expects": {
                            "inputs": [
                                {
                                    "name": "packet",
                                    "from": "intake.outputs.packet",
                                    "media_type": "application/json",
                                }
                            ],
                            "outputs": [
                                {
                                    "name": "review",
                                    "path": "reviews/technical.md",
                                    "media_type": "text/markdown",
                                }
                            ],
                        },
                    },
                    "plan": {
                        "instructions": ".outcomeci/instructions/plan.md",
                        "needs": ["product", "technical"],
                        "expects": {
                            "inputs": [
                                {
                                    "name": "product",
                                    "from": "product.outputs.review",
                                    "media_type": "text/markdown",
                                },
                                {
                                    "name": "technical",
                                    "from": "technical.outputs.review",
                                    "media_type": "text/markdown",
                                },
                            ],
                            "outputs": [
                                {"name": "plan", "path": "plan.md", "media_type": "text/markdown"}
                            ],
                        },
                    },
                },
            },
            "connections": [],
        },
    }
    path = tmp_path / "outcome.yml"
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    return path


def test_compiles_fan_out_graph_and_effective_agent_policies(tmp_path: Path) -> None:
    compiled = compile_workflow(_workflow(tmp_path))
    assert compiled["graph"]["levels"] == [["intake"], ["product", "technical"], ["plan"]]
    assert compiled["triggers"] == {"manual": {"type": "manual"}}
    assert compiled["instructions"]["orchestrator"]["policy"] == {
        "runner": "codex",
        "model": "gpt-orchestrator",
    }
    assert compiled["instructions"]["phases"]["product"]["policy"] == {
        "runner": "claude",
        "model": "claude-review",
    }
    assert compiled["instructions"]["phases"]["technical"]["policy"] == {
        "runner": "codex",
        "model": "gpt-default",
    }
    assert (
        compiled["instructions"]["schemas"][".outcomeci/schemas/packet.json"]["value"]["type"]
        == "object"
    )


def test_requires_a_trigger(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    del value["spec"]["triggers"]
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    with pytest.raises(ConfigError, match="spec.triggers"):
        compile_workflow(path)


def test_email_trigger_can_feed_a_phase(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    value["spec"]["triggers"] = {
        "inbound_email": {"type": "email.received", "filters": {"subject_prefix": "Proof"}}
    }
    value["spec"]["agents"]["phases"]["intake"]["expects"]["inputs"][0]["from"] = (
        "trigger.inbound_email"
    )
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    compiled = compile_workflow(path)
    assert compiled["triggers"]["inbound_email"]["type"] == "email.received"


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda value: value["spec"]["agents"]["phases"]["plan"].update(needs=["missing"]),
            "unknown phase",
        ),
        (lambda value: value["spec"]["agents"]["phases"]["intake"].update(needs=["plan"]), "cycle"),
        (
            lambda value: value["spec"]["agents"]["phases"]["plan"]["expects"]["inputs"][0].update(
                {"from": "missing.outputs.review"}
            ),
            "no declared producer",
        ),
        (
            lambda value: value["spec"]["agents"]["phases"]["plan"]["expects"]["outputs"][0].update(
                path="../plan.md"
            ),
            "remain within",
        ),
    ],
)
def test_rejects_invalid_graph_contracts(tmp_path: Path, mutation, error: str) -> None:
    path = _workflow(tmp_path)
    value = yaml.safe_load(path.read_text())
    mutation(value)
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    with pytest.raises(ConfigError, match=error):
        compile_workflow(path)


def test_yaml_mapping_order_does_not_change_revision(tmp_path: Path) -> None:
    path = _workflow(tmp_path)
    first = compile_workflow(path)["workflow_revision"]
    value = yaml.safe_load(path.read_text())
    value["spec"]["agents"]["phases"] = dict(
        reversed(list(value["spec"]["agents"]["phases"].items()))
    )
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    assert compile_workflow(path)["workflow_revision"] == first
