"""The humans block and a step's decision, as the v1 front end reads them."""

from __future__ import annotations

from pathlib import Path

import pytest
import test_v1_slack as slack_example

from outcomeci.workflow.compiler import ConfigError, compile_workflow
from outcomeci.workflow.diagram import workflow_diagram

HUMANS = """\
humans:
  requester: {system: slack, type: user, identifier: trigger.user}
  owner: {system: slack, type: email, identifier: owner@example.com}
  pod: {system: slack, type: group, identifier: "@product-eng"}
  room: {system: slack, type: channel, identifier: "#growth"}
  leads: [owner, requester, {system: slack, type: user, identifier: U0LEAD}]
"""
DISCUSS = "      decided_by: requester  # others may discuss; only the requester approves\n"


def _workflow(tmp_path: Path, humans: str = HUMANS, discuss: str = DISCUSS) -> Path:
    import shutil

    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    path = tmp_path / "wf" / slack_example.WORKFLOW
    text = path.read_text()
    head, _, rest = text.partition("humans:\n")
    _, _, tail = rest.partition("\n\n")
    text = head + humans.rstrip("\n") + "\n\n" + tail
    assert DISCUSS in text
    if "returns:" in discuss:
        text = text.replace("      returns: [plan, status, decision]\n", "")
    path.write_text(text.replace(DISCUSS, discuss))
    return path


def test_humans_lower_to_principals_and_lists(tmp_path):
    compiled = compile_workflow(_workflow(tmp_path))
    humans = compiled["humans"]
    assert humans["requester"] == {
        "system": "slack",
        "type": "user",
        "identifier": {"ref": "trigger.user"},
    }
    assert humans["owner"]["identifier"] == {"literal": "owner@example.com"}
    assert humans["pod"]["identifier"] == {"literal": "@product-eng"}
    assert humans["room"]["identifier"] == {"literal": "#growth"}
    assert humans["leads"] == {
        "type": "list",
        "members": [
            {"human": "owner"},
            {"human": "requester"},
            {"system": "slack", "type": "user", "identifier": {"literal": "U0LEAD"}},
        ],
    }
    # The block is part of the revision, so changing who decides changes the hash.
    other = compile_workflow(_workflow(tmp_path / "other", humans=HUMANS.replace("U0LEAD", "U1")))
    assert other["workflow_revision"] != compiled["workflow_revision"]


def test_a_decision_lowers_with_its_rule_override_and_deadline(tmp_path):
    compiled = compile_workflow(
        _workflow(
            tmp_path,
            discuss=(
                "      decided_by: pod\n      rule: majority\n      override: owner\n"
                "      otherwise: {ask: requester, timeout: 2h}\n"
                "      returns: [plan, status, decision, verdict]\n"
            ),
        )
    )
    block = compiled["instructions"]["steps"]["discuss"]["v1"]["converse"]
    assert block["decision"] == {
        "of": "pod",
        "rule": {"kind": "majority"},
        "override": "owner",
        "otherwise": {"kind": "ask", "ask": "requester", "timeout_seconds": 7200},
    }
    returned = compiled["instructions"]["steps"]["discuss"]["v1"]["returns"]["schema"]
    assert set(returned["properties"]) == {"plan", "status", "decision", "verdict"}
    assert returned["properties"]["verdict"]["required"] == ["outcome", "how", "votes"]


@pytest.mark.parametrize(
    ("discuss", "problem"),
    [
        ("      decided_by: requester\n      rule: 3\n", None),
        ("      decided_by: requester\n      rule: all\n      otherwise: continue\n", None),
        ("      decided_by: requester\n      otherwise: {rule: majority}\n", None),
        ("      decided_by: nobody\n", "decided_by must name a declared human"),
        ("      rule: all\n", "rule needs decided_by"),
        ("      override: owner\n", "override needs decided_by"),
        ("      otherwise: continue\n", "otherwise needs decided_by"),
        ("      decided_by: requester\n      override: nobody\n", "override must name"),
        ("      decided_by: requester\n      rule: most\n", "rule must be any, majority, all"),
        ("      decided_by: requester\n      rule: 0\n", "rule must be any, majority, all"),
        ("      decided_by: requester\n      rule: true\n", "rule must be any, majority, all"),
        ("      decided_by: requester\n      otherwise: later\n", "otherwise must be stop"),
        (
            "      decided_by: requester\n      otherwise: {ask: nobody}\n",
            "otherwise.ask must name",
        ),
        ("      decided_by: requester\n      otherwise: {ask: owner, timeout: 8d}\n", "at most 7d"),
        ("      decided_by: requester\n      otherwise: {rule: 0}\n", "otherwise.rule must be"),
        (
            "      decided_by: requester\n      otherwise: {ask: owner, rule: any}\n",
            "otherwise must",
        ),
        ("      decided_by: requester\n      by: trigger.user\n", "unsupported fields"),
        ("      decided_by: requester\n      returns: [plan, verdicts]\n", "returns lists plan"),
    ],
)
def test_a_decision_is_checked(tmp_path, discuss, problem):
    path = _workflow(tmp_path, discuss=discuss)
    if problem is None:
        compile_workflow(path)
    else:
        with pytest.raises(ConfigError, match=problem):
            compile_workflow(path)


@pytest.mark.parametrize(
    ("humans", "problem"),
    [
        (
            "humans:\n  requester: {system: github, type: user, identifier: trigger.user}\n",
            "uses slack",
        ),
        (
            "humans:\n  requester: {system: nope, type: user, identifier: trigger.user}\n",
            "uses slack",
        ),
        (
            "humans:\n  requester: {system: slack, type: person, identifier: trigger.user}\n",
            "type must be",
        ),
        (
            "humans:\n  requester: {system: slack, type: user, identifier: ''}\n",
            "identifier must be",
        ),
        (
            "humans:\n  requester: {system: slack, type: user}\n",
            "exactly system, type and identifier",
        ),
        (
            "humans:\n  requester: {system: slack, type: user, identifier: trigger.user, x: 1}\n",
            "exactly",
        ),
        ("humans:\n  requester: []\n", "lists at least one human"),
        (
            "humans:\n  requester: [owner]\n  owner: {system: slack, type: user, identifier: U1}\n",
            "not declared above it",
        ),
        ("humans:\n  requester: [{system: slack, type: user}]\n", "exactly system"),
        (
            "humans:\n  Requester: {system: slack, type: user, identifier: trigger.user}\n",
            "lowercase identifier",
        ),
        ("humans: [a]\n", "humans must be a mapping"),
    ],
)
def test_humans_are_checked(tmp_path, humans, problem):
    with pytest.raises(ConfigError, match=problem):
        compile_workflow(_workflow(tmp_path, humans=humans))


def test_steps_reference_humans_by_name_id_or_users(tmp_path):
    path = _workflow(tmp_path)
    text = path.read_text().replace(
        "        - slack.post: {channel: trigger.channel, thread_ts: trigger.ts}",
        "        - slack.post: {channel: humans.room.id, thread_ts: trigger.ts}",
    )
    text = text.replace(
        "      with: [implement, discuss.plan]",
        "      with: [implement, discuss.plan, humans.leads.users, humans.owner]",
    )
    path.write_text(text)
    compiled = compile_workflow(path)
    announce = compiled["instructions"]["steps"]["announce"]["v1"]
    assert announce["grants"][0]["args"]["channel"]["ref"] == "humans.room.id"
    assert [item["ref"] for item in announce["inputs"]] == [
        "implement",
        "discuss.plan",
        "humans.leads.users",
        "humans.owner",
    ]


@pytest.mark.parametrize(
    "reference", ["humans.nobody", "humans.room.name", "humans", "humans.room.id.more"]
)
def test_an_unknown_human_reference_is_refused(tmp_path, reference):
    path = _workflow(tmp_path)
    path.write_text(
        path.read_text().replace(
            "      with: [implement, discuss.plan]", f"      with: [implement, {reference}]"
        )
    )
    with pytest.raises(ConfigError, match="humans"):
        compile_workflow(path)


def test_humans_is_a_reserved_step_name(tmp_path):
    path = _workflow(tmp_path)
    path.write_text(path.read_text().replace("  - announce:", "  - humans:"))
    with pytest.raises(ConfigError, match="reserved"):
        compile_workflow(path)


def test_the_diagram_shows_who_decides(tmp_path):
    compiled = compile_workflow(
        _workflow(
            tmp_path,
            discuss="      decided_by: pod\n      rule: majority\n      override: owner\n",
        )
    )
    gates = {
        node["id"]: node["gate"] for node in workflow_diagram(compiled)["nodes"] if node.get("gate")
    }
    assert gates["discuss"]["decision"] == {"of": "pod", "rule": "majority", "override": "owner"}
