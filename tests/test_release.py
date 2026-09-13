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


def test_homebrew_release_targets_outcomeci_package_and_tap() -> None:
    source = (ROOT / ".github/workflows/homebrew.yml").read_text()
    assert "pypi.org/pypi/outcomeci-cli/" in source
    assert "repository: outcomeci/homebrew-tap" in source
    assert "Formula/outcomeci-cli.rb" in source
    formula = (ROOT / "packaging/outcomeci-cli.rb").read_text()
    assert "class OutcomeciCli < Formula" in formula
    assert 'shell_output("#{bin}/oci --version")' in formula
