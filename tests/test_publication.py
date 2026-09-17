import json
from pathlib import Path

import pytest

from outcomeci import publication
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize


def test_publication_agent_must_sanitize_and_compile(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source, "filesystem")
    workflow = source / "outcome.yml"
    content = workflow.read_text().replace("requester", "izzy")
    workflow.write_text(content)
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        config = destination / "outcome.yml"
        config.write_text(config.read_text().replace("izzy", "requester"))
        (destination / ".outcomeci/publication-requirements.json").write_text(
            json.dumps(
                [
                    {
                        "id": "requester",
                        "json_path": "$.spec.agents.phases.intake.humans.before[0].participant",
                        "kind": "identity",
                        "description": "Person requesting the outcome",
                        "required": True,
                    }
                ]
            )
        )
        (destination / ".outcomeci/publication-report.json").write_text(
            json.dumps(
                [
                    {
                        "requirement": "requester",
                        "files": ["outcome.yml"],
                        "reason": "Makes the requester portable",
                    }
                ]
            )
        )

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(
        workflow, destination, agent="codex", sensitive_terms=["izzy"]
    )
    assert result["compiler_version"] == "1"
    assert len(result["package_digest"]) == 64
    assert result["requirements"][0]["id"] == "requester"


def test_publication_blocks_agent_that_leaves_pii(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source, "filesystem")
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        (destination / ".outcomeci/instructions/standup.md").write_text("Contact izzy@example.com")
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    with pytest.raises(ExecutionError, match="email address"):
        publication.prepare_publication(source / "outcome.yml", destination, agent="codex")


def test_publication_preserves_custom_workflow_filename(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source, "filesystem")
    workflow = source / "custom-workflow.yml"
    (source / "outcome.yml").rename(workflow)
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(workflow, destination, agent="codex")
    assert result["workflow_file"] == "custom-workflow.yml"


def test_publication_repairs_an_invalid_first_candidate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source, "filesystem")
    destination = tmp_path / "public"
    attempts = 0

    def invoke(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        manifest = "{}" if attempts == 1 else "[]"
        (destination / ".outcomeci/publication-requirements.json").write_text(manifest)
        (destination / ".outcomeci/publication-report.json").write_text(manifest)

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(source / "outcome.yml", destination, agent="codex")

    assert attempts == 2
    assert result["requirements"] == []


def test_publication_blocks_original_vault_path_left_by_agent(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source, "filesystem")
    workflow = source / "outcome.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "spec:\n", "spec:\n  credential_ref: vault://brickbuds/slack\n", 1
        )
    )
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    with pytest.raises(ExecutionError, match="configured sensitive term"):
        publication.prepare_publication(workflow, destination, agent="codex")


def test_publication_replaces_vault_path_with_consumer_requirement(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    initialize(source, "filesystem")
    workflow = source / "outcome.yml"
    workflow.write_text(
        workflow.read_text().replace(
            "  connections: []",
            """  connections:
    slack:
      provider: http
      base_url: https://slack.com
      auth:
        type: bearer
        credential: vault:brickbuds/production/slack-token""",
        )
    )
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        config = destination / "outcome.yml"
        config.write_text(
            config.read_text()
            .replace("vault:brickbuds/production/slack-token", "vault:slack/bot-token")
            .replace("https://slack.com", "https://api.example.com")
        )
        (destination / ".outcomeci/publication-requirements.json").write_text(
            json.dumps(
                [
                    {
                        "id": "slack_bot_token",
                        "json_path": "$.spec.connections.slack.auth.credential",
                        "kind": "vault",
                        "description": "Slack bot token credential",
                        "required": True,
                    },
                    {
                        "id": "slack_api_endpoint",
                        "json_path": "$.spec.connections.slack.base_url",
                        "kind": "endpoint",
                        "description": "Slack-compatible API endpoint",
                        "required": True,
                    },
                ]
            )
        )
        (destination / ".outcomeci/publication-report.json").write_text(
            json.dumps(
                [
                    {
                        "requirement": "slack_bot_token",
                        "files": ["outcome.yml"],
                        "reason": "Requires each consumer to provide their own Slack token",
                    },
                    {
                        "requirement": "slack_api_endpoint",
                        "files": ["outcome.yml"],
                        "reason": "Makes the API endpoint explicit for each consumer",
                    },
                ]
            )
        )

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(workflow, destination, agent="codex")
    assert result["requirements"][0] == {
        "id": "slack_bot_token",
        "json_path": "$.spec.connections.slack.auth.credential",
        "kind": "vault",
        "description": "Slack bot token credential",
        "required": True,
    }
    public_content = (destination / result["workflow_file"]).read_text()
    assert "vault:brickbuds/production/slack-token" not in public_content
    assert "vault:slack/bot-token" in public_content
