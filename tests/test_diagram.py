"""The workflow diagram: a static, credential-free projection of a compiled workflow."""

import json
from pathlib import Path

import pytest

from outcomeci.config import compile_workflow
from outcomeci.diagram import SCHEMA_VERSION, workflow_diagram

FIXTURES = Path(__file__).parent / "fixtures" / "diagram"
EXAMPLES = ["sentry-triage-fix", "x-engagement"]


def diagram(name: str) -> dict:
    return workflow_diagram(compile_workflow(FIXTURES / name / "outcome.yml"))


@pytest.mark.parametrize("name", EXAMPLES)
def test_matches_the_recorded_diagram(name):
    """The fixtures double as the dashboard's development data; regenerate
    with `python tests/test_diagram.py` after an intended change."""
    expected = json.loads((FIXTURES / f"{name}.diagram.json").read_text())
    actual = diagram(name)
    expected.pop("workflow_revision")
    assert actual.pop("workflow_revision")
    assert actual == expected


@pytest.mark.parametrize("name", EXAMPLES)
def test_is_deterministic_and_never_carries_credentials(name):
    first, second = diagram(name), diagram(name)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    text = json.dumps(first)
    assert first["schema_version"] == SCHEMA_VERSION
    assert "vault:" not in text
    assert "credential" not in text
    assert "secrets." not in text


def test_sentry_triage_fix_shape():
    graph = diagram("sentry-triage-fix")
    assert graph["trigger"] == {
        "id": "trigger",
        "type": "webhook",
        "provider": None,
        "label": "Webhook",
    }
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert [node["id"] for node in graph["nodes"]] == [
        "triage",
        "draft",
        "discuss",
        "fix",
        "report",
    ]
    assert nodes["triage"]["kind"] == "model"
    assert nodes["triage"]["model"]["provider"] == "anthropic"
    assert "runner" not in nodes["triage"]
    assert nodes["draft"]["kind"] == "agent" and nodes["draft"]["runner"] == "claude"
    assert [(c["provider"], c["operations"]) for c in nodes["draft"]["connectors"]] == [
        ("github", ["read"]),
        ("slack", ["post"]),
    ]
    gate = nodes["discuss"]["gate"]
    assert (gate["type"], gate["provider"], gate["signal"]) == ("converse", "slack", "thread")
    assert gate["max_turns"] == 40 and gate["timeout_seconds"] == 7 * 86400
    # Three github.write grants, one per repo, are one connector entry.
    assert nodes["fix"]["connectors"] == [
        {"api": "github", "provider": "github", "operations": ["write"]}
    ]
    assert nodes["fix"]["reviewed"] is True and nodes["draft"]["reviewed"] is False
    edges = {(edge["from"], edge["to"]): edge for edge in graph["edges"]}
    assert edges[("triage", "discuss")] == {
        "from": "triage",
        "to": "discuss",
        "kind": "condition",
        "label": "triage.decision != ignore",
    }
    assert edges[("draft", "discuss")]["kind"] == "data"
    assert ("trigger", "draft") not in edges


def test_x_engagement_shape():
    graph = diagram("x-engagement")
    assert graph["trigger"]["schedule"] == {
        "expression": "5 12 * * *",
        "timezone": "America/Chicago",
    }
    nodes = {node["id"]: node for node in graph["nodes"]}
    # The alias names the account; the icon keys on the connector it uses.
    assert nodes["scan"]["connectors"] == [
        {"api": "x_company", "provider": "x", "operations": ["search_recent"]}
    ]
    assert nodes["scan"]["model"]["fallback"] == {"provider": "openai", "model": "openai/gpt-5.5"}
    assert nodes["share_replies"]["for_each"] == {"over": "digest.candidates", "as": "candidate"}
    assert nodes["share_replies"]["condition"] == {
        "ref": "share_header.posted",
        "op": "==",
        "value": True,
    }
    labels = {(e["from"], e["to"]): e["label"] for e in graph["edges"]}
    assert labels[("share_header", "share_replies")] == "share_header.posted == true"


@pytest.mark.parametrize("name", EXAMPLES)
def test_edges_point_forward_without_duplicates(name):
    graph = diagram(name)
    order = {"trigger": -1, **{node["id"]: node["order"] for node in graph["nodes"]}}
    pairs = [(edge["from"], edge["to"]) for edge in graph["edges"]]
    assert len(pairs) == len(set(pairs))
    assert all(order[source] < order[target] for source, target in pairs)
    # Every step is reachable from the trigger.
    reached = {"trigger"}
    for source, target in pairs:
        if source in reached:
            reached.add(target)
    assert reached == set(order)


def test_a_verified_webhook_names_its_provider_and_events():
    compiled = compile_workflow(FIXTURES / "x-engagement" / "outcome.yml")
    compiled["triggers"] = {
        "webhook": {
            "type": "webhook.received",
            "delivery": "queued",
            "receiver": {
                "uses": "slack",
                "secret": "vault:slack/signing",
                "events": ["mention", "dm"],
            },
        }
    }
    trigger = workflow_diagram(compiled)["trigger"]
    assert trigger == {
        "id": "trigger",
        "type": "webhook",
        "provider": "slack",
        "label": "Slack events",
        "events": ["dm", "mention"],
    }
    assert "vault:" not in json.dumps(trigger)


def test_an_await_step_is_a_reaction_gate():
    compiled = compile_workflow(FIXTURES / "sentry-triage-fix" / "outcome.yml")
    block = compiled["instructions"]["steps"]["discuss"]["v1"]
    block.pop("converse")
    block.update(
        kind="await",
        **{"await": {"api": "slack", "emoji": "white_check_mark", "timeout_seconds": 3600}},
    )
    node = next(n for n in workflow_diagram(compiled)["nodes"] if n["id"] == "discuss")
    assert node["kind"] == "await"
    assert node["gate"] == {
        "type": "await",
        "provider": "slack",
        "signal": "reaction",
        "emoji": "white_check_mark",
        "timeout_seconds": 3600,
    }


def test_an_unknown_step_kind_passes_through():
    compiled = compile_workflow(FIXTURES / "sentry-triage-fix" / "outcome.yml")
    compiled["instructions"]["steps"]["report"]["v1"]["kind"] = "dispatch"
    node = next(n for n in workflow_diagram(compiled)["nodes"] if n["id"] == "report")
    assert node["kind"] == "dispatch" and "runner" not in node


if __name__ == "__main__":
    for example in EXAMPLES:
        (FIXTURES / f"{example}.diagram.json").write_text(
            json.dumps(diagram(example), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
