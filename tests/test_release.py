from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_package_uses_tag_derived_semantic_versions() -> None:
    source = (ROOT / "pyproject.toml").read_text()
    assert 'name = "outcomeci-cli"' in source
    assert 'dynamic = ["version"]' in source
    assert 'source = "vcs"' in source
    assert 'tag_format = "v${version}"' in source


def test_publish_uses_trusted_publishing() -> None:
    source = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "id-token: write" in source
    assert "pypa/gh-action-pypi-publish@release/v1" in source
    assert "environment: pypi" in source
    publish_job = source.split("  publish:\n", 1)[1].split("  release:\n", 1)[0]
    assert "configure-aws-credentials" not in publish_job
    assert "spareparts-changelog" not in source
    assert "gh release create" in source and "--generate-notes" in source
    assert "codeartifact login --tool pip" in source
    assert "python -m build" not in source
    assert "gh workflow run homebrew.yml" in source


def test_merge_release_stays_internal_until_promotion() -> None:
    semantic = (ROOT / ".github/workflows/semantic-release.yml").read_text()
    internal = (ROOT / ".github/workflows/internal-release.yml").read_text()
    assert "gh workflow run publish.yml" not in semantic
    assert "gh workflow run internal-release.yml" in semantic
    assert "proof-runner" not in semantic
    assert "workflow_dispatch:" in internal
    assert "ref: ${{ inputs.tag || github.ref }}" in internal
    assert "codeartifact login --tool twine" in internal
    assert "twine upload --repository codeartifact" in internal
    assert "codeartifact describe-package-version" in internal
    assert "--skip-existing" not in internal


def test_outcome_runner_uses_only_immutable_ecr_tags() -> None:
    source = (ROOT / ".github/workflows/runner-container.yml").read_text()
    ecr_metadata = source.split("Generate immutable ECR image metadata", 1)[1].split(
        "Publish immutable image", 1
    )[0]

    assert "type=sha" in ecr_metadata
    assert "type=ref,event=branch" not in ecr_metadata
    assert 'tags: ["v*"]' not in source


def test_a_release_tags_the_commits_runner_image_without_rebuilding() -> None:
    semantic = (ROOT / ".github/workflows/semantic-release.yml").read_text()
    tagging = (ROOT / ".github/workflows/runner-release-tag.yml").read_text()
    assert "gh workflow run runner-release-tag.yml" in semantic
    assert "docker buildx imagetools create" in tagging
    assert "build-push-action" not in tagging
    assert "ghcr.io/outcomeci/outcome-runner" in tagging
    assert "outcomeci-outcome-runner" in tagging


def test_homebrew_release_targets_outcomeci_package_and_tap() -> None:
    source = (ROOT / ".github/workflows/homebrew.yml").read_text()
    assert "pypi.org/pypi/outcomeci-cli/" in source
    assert "repository: outcomeci/homebrew-tap" in source
    assert "Formula/outcomeci-cli.rb" in source
    assert 'gh pr merge --repo outcomeci/homebrew-tap "$url" --squash' in source
    assert "--auto --squash" not in source
    formula = (ROOT / "packaging/outcomeci-cli.rb").read_text()
    assert "class OutcomeciCli < Formula" in formula
    assert 'shell_output("#{bin}/oci --version")' in formula
