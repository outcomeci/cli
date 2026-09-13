from __future__ import annotations

import json
from pathlib import Path

import yaml

from outcomeci import humans
from outcomeci.repository import initialize
from outcomeci.slack import register_connection


def test_assign_writes_only_readable_slack_selectors(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    register_connection(tmp_path / "outcome.yml")
    result = humans.assign(
        tmp_path,
        tmp_path / "outcome.yml",
        "intake",
        "after",
        "confirm_intent",
        [("user", "@isaah"), ("channel", "#product"), ("group", "design")],
        "ask",
        900,
    )
    hook = yaml.safe_load((tmp_path / "outcome.yml").read_text())["spec"]["agents"]["phases"][
        "intake"
    ]["integrations"][0]
    assert result["targets"] == [
        {"kind": "user", "name": "isaah"},
        {"kind": "channel", "name": "product"},
        {"kind": "group", "name": "design"},
    ]
    assert hook["wait"] == {"strategy": "ask", "timeout_seconds": 900}
    assert "U0" not in (tmp_path / "outcome.yml").read_text()


def test_poll_persists_readable_responses_without_provider_ids(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".outcomeci/outcomes/run-1/interactions/plan/review.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "phase": "plan",
                "id": "review",
                "status": "pending",
                "delivery": {"type": "slack", "connection": "slack_local"},
            }
        )
    )
    monkeypatch.setattr(
        humans,
        "poll_replies",
        lambda *args: [{"from": "Isaah", "message": "Proceed", "responded_at": "1.2"}],
    )
    result = humans.poll(tmp_path, tmp_path / "outcome.yml", "run-1", "review")
    assert result["status"] == "responded"
    assert result["responses"][0]["from"] == "Isaah"
    assert json.loads(path.read_text())["observed_responses"] == result["responses"]


def test_request_delivers_existing_pending_hook_once(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    register_connection(tmp_path / "outcome.yml")
    humans.assign(
        tmp_path,
        tmp_path / "outcome.yml",
        "intake",
        "after",
        "confirm_intent",
        [("user", "isaah")],
        "ask",
        None,
    )
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    interaction = outcome / "interactions/intake/confirm_intent.json"
    interaction.parent.mkdir(parents=True)
    interaction.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "phase": "intake",
                "id": "confirm_intent",
                "status": "pending",
                "delivery": {"type": "slack", "targets": [{"kind": "user", "name": "isaah"}]},
                "wait": {"strategy": "ask"},
            }
        )
    )
    (outcome / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "awaiting_input",
                "pending_interaction": {"id": "confirm_intent", "path": str(interaction)},
            }
        )
    )
    monkeypatch.setattr(
        humans,
        "deliver_slack",
        lambda *args: {
            "delivered": True,
            "targets": [{"kind": "user", "name": "isaah"}],
            "threads": 1,
        },
    )
    result = humans.request(tmp_path, tmp_path / "outcome.yml", "run-1", "confirm_intent")
    assert result["threads"] == 1
    assert json.loads(interaction.read_text())["delivery_status"] == {
        "delivered": True,
        "threads": 1,
    }


def test_request_can_continue_with_durable_open_interaction(tmp_path: Path, monkeypatch) -> None:
    initialize(tmp_path, "filesystem")
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    interaction = outcome / "interactions/plan/expert.json"
    interaction.parent.mkdir(parents=True)
    interaction.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "phase": "plan",
                "id": "expert",
                "status": "pending",
                "delivery": {"type": "slack", "targets": [{"kind": "channel", "name": "product"}]},
                "wait": {"strategy": "ask"},
            }
        )
    )
    run_path = outcome / "run.json"
    run_path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "awaiting_input",
                "pending_interaction": {"id": "expert", "path": str(interaction)},
            }
        )
    )
    monkeypatch.setattr(
        humans,
        "deliver_slack",
        lambda *args: {
            "delivered": True,
            "targets": [{"kind": "channel", "name": "product"}],
            "threads": 1,
        },
    )
    humans.request(tmp_path, tmp_path / "outcome.yml", "run-1", "expert", True)
    state = json.loads(run_path.read_text())
    assert state["status"] == "running"
    assert "pending_interaction" not in state
    assert state["open_interactions"][0]["id"] == "expert"


def test_accept_records_response_without_launching_nested_agent(
    tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, "filesystem")
    outcome = tmp_path / ".outcomeci/outcomes/run-1"
    interaction = outcome / "interactions/plan/expert.json"
    interaction.parent.mkdir(parents=True)
    interaction.write_text(
        json.dumps(
            {
                "id": "expert",
                "phase": "plan",
                "timing": "during",
                "interaction": "consultation",
                "status": "pending",
            }
        )
    )
    run_path = outcome / "run.json"
    run_path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "phase": "plan",
                "status": "awaiting_input",
                "pending_interaction": {
                    "id": "expert",
                    "path": str(interaction),
                    "phase": "plan",
                    "timing": "during",
                },
            }
        )
    )
    monkeypatch.setattr(
        "outcomeci.local.invoke",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nested agent launched")),
    )
    state = humans.accept(
        tmp_path, tmp_path / "outcome.yml", "run-1", "expert", "Use the existing navigation."
    )
    assert state["status"] == "running"
    assert (
        json.loads(interaction.read_text())["response"]["message"] == "Use the existing navigation."
    )
