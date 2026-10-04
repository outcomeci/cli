import json
from pathlib import Path

import pytest

from outcomeci import publication
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize

OVERVIEW = """# Workflow overview

## Purpose
Run the configured planning and implementation steps for a repository task.

## Steps
The agent follows the packaged instructions to plan and implement the task.

## Inputs and setup
Provide the repository task and configure the required GitHub credential.

## Outputs
The workflow produces the artifacts specified by its implementation instructions.
"""


def write_overview(destination: Path) -> None:
    (destination / publication.OVERVIEW).write_text(OVERVIEW, encoding="utf-8")


def test_publication_agent_must_sanitize_and_compile(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    workflow = source / "outcome.yml"
    workflow.write_text(workflow.read_text().replace("vault:github", "vault:izzy/github"))
    destination = tmp_path / "public"

    def invoke(*_args, **_kwargs):
        write_overview(destination)
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
        write_overview(destination)
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
        write_overview(destination)
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
        write_overview(destination)
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
        write_overview(destination)
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
        write_overview(destination)
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
        write_overview(destination)
        (destination / ".outcomeci/publication-requirements.json").write_text("[]")
        (destination / ".outcomeci/publication-report.json").write_text("[]")

    monkeypatch.setattr(publication, "invoke", invoke)
    with pytest.raises(ExecutionError, match="outcome.yml: configured sensitive term"):
        publication.prepare_publication(source / "outcome.yml", destination, agent="codex")


@pytest.mark.parametrize(
    "content",
    [
        "Too short",
        "x" * 12001,
        "\xff".encode("latin1"),
        OVERVIEW + "<script>alert(1)</script>",
        OVERVIEW + "[Read more](https://example.com)",
        OVERVIEW + "![Preview](image.png)",
        OVERVIEW + "[Read more][reference]",
        OVERVIEW + "\n[reference]: target.md",
        OVERVIEW + "https://example.com",
        OVERVIEW + "<https://example.com>",
    ],
)
def test_overview_rejects_invalid_content(tmp_path: Path, content) -> None:
    path = tmp_path / publication.OVERVIEW
    path.parent.mkdir()
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    with pytest.raises(ExecutionError, match="publication overview"):
        publication._validate_overview(tmp_path)


def test_overview_is_required(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="UTF-8 Markdown"):
        publication._validate_overview(tmp_path)


def test_overview_is_included_in_package_digest(tmp_path: Path) -> None:
    (tmp_path / ".outcomeci").mkdir()
    write_overview(tmp_path)
    before = publication._package_digest(tmp_path)
    assert publication._validate_overview(tmp_path) == OVERVIEW
    (tmp_path / publication.OVERVIEW).write_text(OVERVIEW + "\nAdditional factual detail.")
    assert before != publication._package_digest(tmp_path)


@pytest.mark.parametrize("leak", ["person@example.com", "C123456789", "private-project"])
def test_overview_is_checked_for_private_values(tmp_path: Path, leak: str) -> None:
    (tmp_path / ".outcomeci").mkdir()
    (tmp_path / publication.OVERVIEW).write_text(OVERVIEW + leak)
    with pytest.raises(ExecutionError, match="publication-overview.md"):
        publication._privacy_gate(tmp_path, ["private-project"])


def test_publication_repairs_missing_overview(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    initialize(source)
    destination = tmp_path / "public"
    attempts = []

    def invoke(_agent, _model, prompt, *_args, **_kwargs):
        attempts.append(prompt)
        config = destination / "outcome.yml"
        config.write_text(config.read_text().replace("vault:github", "vault:shared/github"))
        (destination / publication.REQUIREMENTS).write_text("[]")
        (destination / publication.REPORT).write_text("[]")
        if len(attempts) == 2:
            write_overview(destination)

    monkeypatch.setattr(publication, "invoke", invoke)
    publication.prepare_publication(source / "outcome.yml", destination, agent="codex")
    assert len(attempts) == 2
    assert all("publication-overview.md" in prompt for prompt in attempts)


def slack_publication_source(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / ".outcomeci").mkdir()
    source = root / "outcome.yml"
    source.write_text("""apiVersion: outcomeci.workflow/v1
trigger: manual
secrets:
  slack: vault:private/slack
apis:
  slack: {uses: slack, auth: secrets.slack}
steps:
  - announce:
      reason: Post the supplied message to the triggering channel.
      can:
        - slack.post: {channel: trigger.channel}
""")
    return source


def sanitize_slack_package(_agent, _model, _prompt, destination, *_args, **_kwargs):
    (destination / ".outcomeci").mkdir(exist_ok=True)
    config = destination / "outcome.yml"
    config.write_text(config.read_text().replace("vault:private/slack", "vault:shared/slack"))
    (destination / publication.REQUIREMENTS).write_text("[]")
    (destination / publication.REPORT).write_text("[]")
    write_overview(destination)


def test_runtime_channel_reference_is_not_a_private_literal(tmp_path):
    source = slack_publication_source(tmp_path / "source")
    assert publication._consumer_values(source) == ["vault:private/slack"]
    source.write_text(source.read_text().replace("trigger.channel", "C123456789"))
    assert "C123456789" in publication._consumer_values(source)


def test_publication_preserves_runtime_behavior_and_platform_syntax(tmp_path, monkeypatch):
    source = slack_publication_source(tmp_path / "source")
    destination = tmp_path / "public"
    monkeypatch.setattr(publication, "invoke", sanitize_slack_package)
    result = publication.prepare_publication(
        source, destination, agent="codex", sensitive_terms=["outcomeci"]
    )
    content = (destination / "outcome.yml").read_text()
    assert "trigger.channel" in content
    assert "outcomeci.workflow/v1" in content
    assert "vault:private/slack" not in content
    assert len(result["package_digest"]) == 64


@pytest.mark.parametrize(
    "private_content",
    [
        "This belongs to the outcomeci workspace",
        ".outcomeci/instructions/outcomeci-private.md",
        "private-outcomeci.workflow/v1",
        "outcomeci.workflow/v1-private",
    ],
)
def test_namespace_exception_does_not_hide_consumer_content(tmp_path, private_content):
    (tmp_path / "instructions.md").write_text(private_content)
    with pytest.raises(ExecutionError, match="sensitive term"):
        publication._privacy_gate(tmp_path, ["outcomeci"])


@pytest.mark.parametrize("path", [publication.REQUIREMENTS, publication.REPORT])
def test_privacy_checks_cover_public_manifests(tmp_path, path):
    (tmp_path / ".outcomeci").mkdir()
    (tmp_path / path).write_text(json.dumps([{"description": "person@example.com"}]))
    with pytest.raises(ExecutionError, match="email address"):
        publication._privacy_gate(tmp_path, [])


def test_repair_receives_validation_reason_and_failure_has_safe_category(tmp_path, monkeypatch):
    source = slack_publication_source(tmp_path / "source")
    destination = tmp_path / "public"
    prompts = []

    def invoke(_agent, _model, prompt, *args, **kwargs):
        prompts.append(prompt)
        sanitize_slack_package(_agent, _model, prompt, *args, **kwargs)
        (destination / publication.REQUIREMENTS).write_text("{}")

    monkeypatch.setattr(publication, "invoke", invoke)
    with pytest.raises(publication.PublicationValidationError) as caught:
        publication.prepare_publication(source, destination, agent="codex")
    assert caught.value.category == "publication_manifest_invalid"
    assert "publication manifests must be JSON arrays" in prompts[1]
    assert "trigger.channel" not in prompts[0].split("Private literal values to remove")[1]
