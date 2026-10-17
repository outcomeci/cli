"""Resolving a run's humans through Slack, and reading them from steps."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import test_multi_user_converse as pod_example
import test_v1_slack as slack_example

from outcomeci.broker import executor as integrations
from outcomeci.runtime import humans
from outcomeci.runtime.process import ExecutionError
from outcomeci.runtime.steps import MISSING, value
from outcomeci.workflow.compiler import compile_workflow

PM, ENG1, ENG2 = pod_example.PM, pod_example.ENG1, pod_example.ENG2


class Directory(pod_example.Pod):
    """The Slack lookups alone, with a failure the test can script."""

    def __init__(self, fail: str | None = None):
        super().__init__([])
        self.fail = fail

    def __call__(self, request):
        if self.fail and request.url.path == self.fail:
            return httpx.Response(500, json={"ok": False, "error": "fatal_error"})
        return super().__call__(request)


def _run(tmp_path: Path, monkeypatch, humans_yaml: str, slack: Directory | None = None):
    import shutil

    slack = slack or Directory()
    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    if "  requester:" not in humans_yaml:
        humans_yaml += "  requester: {system: slack, type: user, identifier: trigger.user}\n"
    pod_example._variant(tmp_path / "wf", humans_yaml, "      decided_by: requester\n")
    real = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(slack)}),
    )
    compiled = compile_workflow(tmp_path / "wf" / slack_example.WORKFLOW)
    state = {"run_id": "run", "trigger": {"type": "webhook.received", "value": {"user": PM}}}
    return tmp_path / "wf", compiled, state, slack


def _resolve(root, compiled, state):
    return humans.resolve(root, compiled, state, lambda reference: "xoxb-test")


EVERY_KIND = f"""\
  requester: {{system: slack, type: user, identifier: trigger.user}}
  owner: {{system: slack, type: email, identifier: {pod_example.OWNER_EMAIL}}}
  pod: {{system: slack, type: group, identifier: "@product-eng"}}
  pod_by_id: {{system: slack, type: group, identifier: S0POD}}
  room: {{system: slack, type: channel, identifier: "#growth"}}
  room_by_id: {{system: slack, type: channel, identifier: C0BUILD}}
  leads: [owner, pod, {{system: slack, type: user, identifier: U0LEAD}}]
"""


def test_every_kind_of_human_resolves_to_people(tmp_path, monkeypatch):
    root, compiled, state, slack = _run(tmp_path, monkeypatch, EVERY_KIND)

    resolved = _resolve(root, compiled, state)

    assert resolved["requester"] == {
        "type": "user",
        "system": "slack",
        "identifier": PM,
        "id": PM,
        "users": [PM],
    }
    assert resolved["owner"]["id"] == PM and resolved["owner"]["users"] == [PM]
    assert resolved["pod"] == {
        "type": "group",
        "system": "slack",
        "identifier": "@product-eng",
        "id": "S0POD",
        "users": [ENG1, ENG2],
    }
    assert resolved["pod_by_id"]["users"] == [ENG1, ENG2]
    assert resolved["room"]["id"] == "C0BUILD"
    # Bots in a channel are not people; members are kept sorted.
    assert resolved["room"]["users"] == sorted([PM, ENG1, ENG2])
    assert resolved["room_by_id"]["users"] == resolved["room"]["users"]
    # A list is the union, in order, without repeats; its members are kept.
    assert resolved["leads"]["users"] == [PM, ENG1, ENG2, "U0LEAD"]
    assert resolved["leads"]["members"][:2] == ["owner", "pod"]
    assert resolved["leads"]["members"][2]["id"] == "U0LEAD"
    # The record is written once and read back on every later call.
    assert json.loads(humans.path(root, "run").read_text()) == resolved
    lookups = len(slack.lookups)
    assert _resolve(root, compiled, state) == resolved
    assert len(slack.lookups) == lookups
    # A group by id and a channel by id skip the listing calls.
    assert slack.lookups.count("/api/usergroups.list") == 1
    assert slack.lookups.count("/api/conversations.list") == 1


def test_steps_read_humans_from_the_run_record(tmp_path, monkeypatch):
    root, compiled, state, _ = _run(tmp_path, monkeypatch, EVERY_KIND)
    assert value(root, state, "humans.pod") is MISSING

    _resolve(root, compiled, state)

    assert value(root, state, "humans.pod.users") == [ENG1, ENG2]
    assert value(root, state, "humans.room.id") == "C0BUILD"
    assert value(root, state, "humans.owner")["id"] == PM
    assert value(root, state, "humans.nobody") is MISSING


@pytest.mark.parametrize(
    ("humans_yaml", "problem"),
    [
        (
            "  owner: {system: slack, type: email, identifier: nobody@example.com}\n",
            "no Slack user has",
        ),
        ('  pod: {system: slack, type: group, identifier: "@nobody"}\n', "no user group is called"),
        ("  pod: {system: slack, type: group, identifier: not-an-id}\n", "no user group is called"),
        (
            '  room: {system: slack, type: channel, identifier: "#nowhere"}\n',
            "no channel is called",
        ),
        (
            "  requester: {system: slack, type: user, identifier: trigger.missing}\n",
            "did not name a person",
        ),
        (
            "  requester: {system: slack, type: user, identifier: owner@example.com}\n",
            "use type email",
        ),
    ],
)
def test_a_human_who_cannot_be_found_fails_the_run(tmp_path, monkeypatch, humans_yaml, problem):
    root, compiled, state, _ = _run(tmp_path, monkeypatch, humans_yaml)

    with pytest.raises(ExecutionError, match=problem):
        _resolve(root, compiled, state)
    assert not humans.path(root, "run").exists()


def test_an_empty_group_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setitem(pod_example.DIRECTORY["group_members"], "S0POD", [])
    root, compiled, state, _ = _run(
        tmp_path, monkeypatch, '  pod: {system: slack, type: group, identifier: "@product-eng"}\n'
    )

    with pytest.raises(ExecutionError, match="has no members"):
        _resolve(root, compiled, state)


def test_a_failed_lookup_names_the_call(tmp_path, monkeypatch):
    root, compiled, state, _ = _run(
        tmp_path,
        monkeypatch,
        '  room: {system: slack, type: channel, identifier: "#growth"}\n',
        Directory(fail="/api/conversations.members"),
    )

    with pytest.raises(ExecutionError, match="humans.room: slack.members failed"):
        _resolve(root, compiled, state)


def test_resolving_needs_a_credential_resolver(tmp_path, monkeypatch):
    root, compiled, state, _ = _run(tmp_path, monkeypatch, EVERY_KIND)

    with pytest.raises(ExecutionError, match="credential resolver"):
        humans.resolve(root, compiled, state, None)


def test_a_workflow_without_humans_resolves_nothing(tmp_path, monkeypatch):
    root, compiled, state, slack = _run(tmp_path, monkeypatch, EVERY_KIND)
    compiled = {key: item for key, item in compiled.items() if key != "humans"}

    assert humans.resolve(root, compiled, state, None) == {}
    assert slack.lookups == []


def test_the_lookups_are_never_offered_to_a_step(tmp_path, monkeypatch):
    """Resolution runs as the runtime; no step's capabilities carry the lookups."""
    root, compiled, state, slack = _run(tmp_path, monkeypatch, EVERY_KIND)

    _resolve(root, compiled, state)

    assert "/api/conversations.members" in slack.lookups
    for name, step in compiled["instructions"]["steps"].items():
        assert not any(cap.split(".", 1)[1] in humans.LOOKUPS for cap in step["capabilities"]), name
