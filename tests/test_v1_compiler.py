from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from outcomeci.config import ConfigError, compile_workflow
from outcomeci.policy import _within
from outcomeci.v1 import duration_seconds, shape_schema

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "v1"


def _write(tmp_path: Path, steps: list, **top) -> Path:
    document = {
        "apiVersion": "outcomeci.com/v1",
        "trigger": "manual",
        "secrets": {"slack": "vault:slack/bot-token", "github": "vault:github/pat"},
        "apis": {
            "slack": {"uses": "slack", "auth": "secrets.slack"},
            "github": {"uses": "github", "auth": "secrets.github"},
        },
        "steps": steps,
        **top,
    }
    path = tmp_path / "demo.outcome.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _step(name: str, **fields) -> dict:
    return {name: {"reason": "Do the thing.", **fields}}


def test_the_sentry_example_compiles_to_a_linear_graph():
    compiled = compile_workflow(EXAMPLES / "sentry-to-github-pr.outcome.yaml")
    assert compiled["api_version"] == "outcomeci.com/v1"
    assert compiled["graph"]["levels"] == [["triage"], ["approve"], ["fix"], ["announce"]]
    assert compiled["source"]["apiVersion"] == "outcomeci.com/v1"
    assert set(compiled["connectors"]) == {"slack", "github"}
    phases = compiled["instructions"]["phases"]
    assert phases["triage"]["path"] == ".outcomeci/instructions/triage-and-notify.md"
    assert phases["announce"]["content"].startswith("Reply in the alert's thread")
    assert phases["fix"]["v1"]["policy"].startswith("One new branch")
    assert phases["approve"]["capabilities"] == ["slack.post", "slack.reactions"]


def test_a_changed_connector_changes_the_revision(monkeypatch):
    from outcomeci import v1

    path = EXAMPLES / "sentry-to-github-pr.outcome.yaml"
    before = compile_workflow(path)["workflow_revision"]
    real = v1.provider

    def changed(name):
        found = real(name)
        return type(found)(**{**found.__dict__, "max_requests": found.max_requests + 1})

    monkeypatch.setattr(v1, "provider", changed)
    assert compile_workflow(path)["workflow_revision"] != before


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([_step("a", can=["jira.read"])], "'jira' is not a declared api"),
        ([_step("a", can=["slack.delete"])], "slack has no operation 'delete'"),
        ([_step("a", can=[{"slack.post": {"user": "x"}}])], "cannot be scoped by 'user'"),
        ([_step("a", **{"from": "b.plan"})], "is not the trigger or an earlier step"),
        (
            [_step("a", returns={"plan": "string"}), _step("b", **{"with": "a.missing"})],
            "step a returns no missing",
        ),
        (
            [_step("a"), _step("b", when="a.calls.slack.post")],
            "step a is not granted slack.post",
        ),
        ([_step("a", can=[{"github.write": {"repo": "not-a-repo"}}])], "owner/name"),
        ([_step("a", reason="missing.md")], "missing.md was not found"),
        ([_step("a", bogus=True)], "unsupported fields for agent steps: bogus"),
        ([_step("a"), _step("a")], "duplicate or reserved step name"),
        ([{"a": {"await": {"slack.reaction": {"message": "trigger"}}}}], "recorded call"),
        ([{"a": {"await": {"github.reaction": {"message": "x"}}}}], "not a watcher"),
    ],
)
def test_compile_errors_name_the_problem(tmp_path, steps, message):
    with pytest.raises(ConfigError, match=message):
        compile_workflow(_write(tmp_path, steps))


def test_unknown_providers_and_secrets_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="no installed connector provides 'jira'"):
        compile_workflow(_write(tmp_path, [_step("a")], apis={"jira": {"uses": "jira"}}))
    with pytest.raises(ConfigError, match="must reference a declared secret"):
        compile_workflow(
            _write(tmp_path, [_step("a")], apis={"slack": {"uses": "slack", "auth": "secrets.x"}})
        )
    with pytest.raises(ConfigError, match="must be a vault: reference"):
        compile_workflow(_write(tmp_path, [_step("a")], secrets={"slack": "xoxb-raw"}))


def test_literal_repos_and_as_names_compile(tmp_path):
    steps = [
        _step("a", can=[{"slack.post": {"channel": "build", "as": "plan_post"}}]),
        _step("b", **{"with": "a.calls.plan_post"}, can=[{"github.write": {"repo": "o/r"}}]),
    ]
    compiled = compile_workflow(_write(tmp_path, steps))
    grant = compiled["instructions"]["phases"]["b"]["v1"]["grants"][0]
    assert grant["args"]["repo"] == {"literal": {"owner": "o", "name": "r"}}
    assert compiled["instructions"]["phases"]["b"]["v1"]["inputs"] == [
        {"name": "plan_post", "ref": "a.calls.plan_post"}
    ]


def test_shapes_compile_to_json_schema():
    assert shape_schema({"title": None, "n": "int", "d?": "enum[fix, no_op]"}, "r") == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "n": {"type": "integer"},
            "d": {"type": "string", "enum": ["fix", "no_op"]},
        },
        "required": ["title", "n"],
    }
    assert shape_schema([{"repo": {"owner": None}}], "r")["items"]["properties"]["repo"][
        "required"
    ] == ["owner"]
    with pytest.raises(ConfigError, match="unknown type 'date'"):
        shape_schema("date", "r")


def test_durations():
    assert duration_seconds("45m", "t") == 2700
    assert duration_seconds("2h", "t") == 7200
    assert duration_seconds(90, "t") == 90
    with pytest.raises(ConfigError):
        duration_seconds("soon", "t")


@pytest.mark.parametrize(
    ("path", "inside"),
    [
        ("/repos/o/r", True),
        ("/repos/o/r/pulls", True),
        ("/repos/O/R/contents/x?ref=main", True),
        ("/repos/o/r2/pulls", False),
        ("/repos/o/r/../x/pulls", False),
        ("/repos/o/r/%2e%2e/x", False),
        ("/repos/o/r//x", False),
        ("/user/repos", False),
        (None, False),
    ],
)
def test_paths_stay_under_the_granted_repository(path, inside):
    assert _within(path, "/repos/o/r") is inside
