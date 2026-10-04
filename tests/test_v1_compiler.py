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
        "apiVersion": "outcomeci.workflow/v1",
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
    assert compiled["api_version"] == "outcomeci.workflow/v1"
    assert compiled["graph"]["levels"] == [["triage"], ["approve"], ["fix"], ["announce"]]
    assert compiled["source"]["apiVersion"] == "outcomeci.workflow/v1"
    assert set(compiled["connectors"]) == {"slack", "github"}
    steps = compiled["instructions"]["steps"]
    assert steps["triage"]["path"] == ".outcomeci/instructions/triage-and-notify.md"
    assert steps["announce"]["content"].startswith("Reply in the alert's thread")
    assert steps["fix"]["v1"]["policy"].startswith("One new branch")
    assert steps["approve"]["capabilities"] == ["slack.post", "slack.reactions"]


def test_a_connection_names_its_credential_and_the_kinds_its_connector_accepts(tmp_path):
    compiled = compile_workflow(_write(tmp_path, [_step("a", can=["github.read"])]))
    connections = {item["ref"]: item for item in compiled["workflow"]["spec"]["connections"]}
    github = connections["github"]["auth"]
    assert github["connector"] == "github"
    assert github["credential"] == "vault:github/pat"
    assert [entry["kind"] for entry in github["accepts"]] == ["token", "app_installation", "oauth2"]
    assert [entry["kind"] for entry in connections["slack"]["auth"]["accepts"]] == [
        "token",
        "oauth2",
    ]
    assert "type" not in github


def test_a_connector_that_needs_a_credential_requires_auth(tmp_path):
    path = _write(tmp_path, [_step("a")], apis={"github": {"uses": "github"}})
    with pytest.raises(ConfigError, match="apis.github.auth is required"):
        compile_workflow(path)


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
        ([_step("a", can=[{"github.search": {"repo": "not-a-repo"}}])], "owner/name"),
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


def test_the_slack_example_triggers_on_signed_mentions_and_dms():
    compiled = compile_workflow(EXAMPLES / "slack-to-github-pr.outcome.yaml")

    assert compiled["triggers"] == {
        "webhook": {
            "type": "webhook.received",
            "delivery": "queued",
            "receiver": {
                "uses": "slack",
                "secret": "vault:slack/signing-secret",
                "events": ["dm", "mention"],
            },
        }
    }


@pytest.mark.parametrize(
    ("webhook", "message"),
    [
        ({"uses": "x", "auth": "secrets.github", "events": ["push"]}, "cannot receive"),
        ({"uses": "slack", "auth": "secrets.missing", "events": ["dm"]}, "declared secret"),
        ({"uses": "slack", "auth": "secrets.slack", "events": ["reaction"]}, "not one of"),
        ({"uses": "slack", "auth": "secrets.slack", "events": []}, "must list"),
        ({"uses": "slack", "auth": "secrets.slack", "events": ["dm"], "x": 1}, "supports uses"),
    ],
)
def test_a_webhook_receiver_is_checked_against_its_provider(tmp_path, webhook, message):
    with pytest.raises(ConfigError, match=message):
        compile_workflow(_write(tmp_path, [_step("a")], trigger={"webhook": webhook}))


def test_literal_repos_and_as_names_compile(tmp_path):
    steps = [
        _step("a", can=[{"slack.post": {"channel": "build", "as": "plan_post"}}]),
        _step("b", **{"with": "a.calls.plan_post"}, can=[{"github.write": {"repo": "o/r"}}]),
    ]
    compiled = compile_workflow(_write(tmp_path, steps))
    grant = compiled["instructions"]["steps"]["b"]["v1"]["grants"][0]
    assert grant["args"]["repo"] == {"literal": {"owner": "o", "name": "r"}}
    assert compiled["instructions"]["steps"]["b"]["v1"]["inputs"] == [
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


def test_a_search_grant_compiles_its_repo_and_the_qualifier_rule(tmp_path):
    compiled = compile_workflow(
        _write(tmp_path, [_step("a", can=[{"github.search": {"repo": "outcomeci/api"}}])])
    )
    (grant,) = compiled["instructions"]["steps"]["a"]["v1"]["grants"]
    assert grant["args"] == {"repo": {"literal": {"owner": "outcomeci", "name": "api"}}}
    search = compiled["workflow"]["spec"]["integrations"]["github"]["operations"]["search"]
    assert search["request"] == {"methods": ["GET"]}
    assert search["policy"]["side_effect"] == "read"
    assert search["grantable"]["repo"]["query_qualifier"]["term"] == "repo:{owner}/{name}"
    assert search["deny"][0]["path"] == "^(?!/search/code$)"


QUERY = {"param": "q", "term": "repo:{owner}/{name}", "exclusive": ["repo"], "operators": []}


@pytest.mark.parametrize(
    "rule",
    [
        {"query_qualifier": QUERY, "path_prefix": "/repos/{owner}/{name}"},
        {"query_qualifier": {**QUERY, "term": "{owner}/{name}"}, "value_fields": ["owner", "name"]},
        {"query_qualifier": {**QUERY, "exclusive": ["org"]}, "value_fields": ["owner", "name"]},
        {"query_qualifier": {**QUERY, "param": ""}, "value_fields": ["owner", "name"]},
        {"query_qualifier": QUERY, "value_fields": ["owner", "repo"]},
        {"query_qualifier": {**QUERY, "operators": "OR"}, "value_fields": ["owner", "name"]},
    ],
)
def test_a_malformed_query_qualifier_is_refused(rule):
    from outcomeci.config import _grantable

    _grantable(
        {"grantable": {"repo": {"query_qualifier": QUERY, "value_fields": ["owner", "name"]}}}, "op"
    )
    with pytest.raises(ConfigError, match="op.grantable.repo"):
        _grantable({"grantable": {"repo": rule}}, "op")


def test_only_an_operation_that_chooses_its_request_scopes_a_query():
    from outcomeci.config import _operation

    with pytest.raises(ConfigError, match="scopes a query"):
        _operation(
            {
                "request": {"method": "GET", "path": "/search"},
                "grantable": {
                    "repo": {"query_qualifier": QUERY, "value_fields": ["owner", "name"]}
                },
            },
            "op",
        )


def test_github_signed_webhook_compiles(tmp_path):
    compiled = compile_workflow(
        _write(
            tmp_path,
            [_step("a")],
            trigger={
                "webhook": {
                    "uses": "github",
                    "auth": "secrets.github",
                    "events": ["issues", "push"],
                }
            },
        )
    )
    receiver = compiled["triggers"]["webhook"]["receiver"]
    assert receiver["uses"] == "github"
    assert receiver["events"] == ["issues", "push"]
