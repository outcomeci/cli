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
    assert "outcomeci/spareparts-changelog@v0" in source
    assert "releases/outcomeci-cli/" in source
    assert "codeartifact login --tool pip" in source
    assert "python -m build" not in source
    assert "gh workflow run homebrew.yml" in source


def test_merge_release_stays_internal_until_promotion() -> None:
    semantic = (ROOT / ".github/workflows/semantic-release.yml").read_text()
    internal = (ROOT / ".github/workflows/internal-release.yml").read_text()
    proof = (ROOT / ".github/workflows/proof-runner-container.yml").read_text()
    assert "gh workflow run publish.yml" not in semantic
    assert "gh workflow run internal-release.yml" in semantic
    assert "gh workflow run proof-runner-container.yml" in semantic
    assert "workflow_dispatch:" in internal
    assert "ref: ${{ inputs.tag || github.ref }}" in internal
    assert "codeartifact login --tool twine" in internal
    assert "workflow_dispatch:" in proof
    assert "REQUESTED_TAG: ${{ inputs.tag }}" in proof
    assert "outcomeci-proof-runner" in proof
    assert "docker push" in proof


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
