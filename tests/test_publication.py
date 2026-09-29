import json
from pathlib import Path

import pytest

from outcomeci import publication
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize


def test_publication_agent_must_sanitize_and_compile(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    workflow = source / "outcome.yml"
    workflow.write_text(workflow.read_text().replace("vault:github", "vault:izzy/github"))
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        config = destination / "outcome.yml"
        config.write_text(config.read_text().replace("vault:izzy/github", "vault:github"))
        (destination / ".outcomeci/publication-requirements.json").write_text(
            json.dumps(
                [
                    {
                        "id": "github_token",
                        "json_path": "$.secrets.github",
                        "kind": "vault",
                        "description": "GitHub token that can read the repository",
                        "required": True,
                    }
                ]
            )
        )
        (destination / ".outcomeci/publication-report.json").write_text(
            json.dumps(
                [
                    {
                        "requirement": "github_token",
                        "files": ["outcome.yml"],
                        "reason": "Each consumer stores their own GitHub token",
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
    assert result["requirements"][0]["id"] == "github_token"


def test_publication_blocks_agent_that_leaves_pii(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        (destination / ".outcomeci/instructions/plan.md").write_text("Contact izzy@example.com")
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    with pytest.raises(ExecutionError, match="email address"):
        publication.prepare_publication(source / "outcome.yml", destination, agent="codex")


def test_publication_preserves_custom_workflow_filename(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    workflow = source / "custom-workflow.yml"
    (source / "outcome.yml").rename(workflow)
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        config = destination / "custom-workflow.yml"
        config.write_text(config.read_text().replace("vault:github", "vault:shared/github"))
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(workflow, destination, agent="codex")
    assert result["workflow_file"] == "custom-workflow.yml"


def test_publication_repairs_an_invalid_first_candidate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    destination = tmp_path / "public"
    attempts = 0

    def invoke(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        manifest = "{}" if attempts == 1 else "[]"
        config = destination / "outcome.yml"
        config.write_text(config.read_text().replace("vault:github", "vault:shared/github"))
        (destination / ".outcomeci/publication-requirements.json").write_text(manifest)
        (destination / ".outcomeci/publication-report.json").write_text(manifest)

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(source / "outcome.yml", destination, agent="codex")

    assert attempts == 2
    assert result["requirements"] == []


def test_publication_blocks_original_vault_path_left_by_agent(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    workflow = source / "outcome.yml"
    workflow.write_text(workflow.read_text().replace("vault:github", "vault:brickbuds/github"))
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
    initialize(source)
    workflow = source / "outcome.yml"
    workflow.write_text(
        workflow.read_text().replace("vault:github", "vault:brickbuds/production/github-token")
    )
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        config = destination / "outcome.yml"
        config.write_text(
            config.read_text().replace("vault:brickbuds/production/github-token", "vault:github")
        )
        (destination / ".outcomeci/publication-requirements.json").write_text(
            json.dumps(
                [
                    {
                        "id": "github_token",
                        "json_path": "$.secrets.github",
                        "kind": "vault",
                        "description": "GitHub token credential",
                        "required": True,
                    },
                ]
            )
        )
        (destination / ".outcomeci/publication-report.json").write_text(
            json.dumps(
                [
                    {
                        "requirement": "github_token",
                        "files": ["outcome.yml"],
                        "reason": "Requires each consumer to provide their own GitHub token",
                    },
                ]
            )
        )

    monkeypatch.setattr(publication, "invoke", invoke)
    result = publication.prepare_publication(workflow, destination, agent="codex")
    assert result["requirements"][0] == {
        "id": "github_token",
        "json_path": "$.secrets.github",
        "kind": "vault",
        "description": "GitHub token credential",
        "required": True,
    }
    public_content = (destination / result["workflow_file"]).read_text()
    assert "vault:brickbuds/production/github-token" not in public_content
    assert "vault:github" in public_content


def test_publication_flags_a_v1_secret_the_agent_left_in_place(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    with pytest.raises(ExecutionError, match="outcome.yml: configured sensitive term"):
        publication.prepare_publication(source / "outcome.yml", destination, agent="codex")
