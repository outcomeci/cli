"""Shared artifact manifest contract for local and managed outcome runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .security import private_path

SCHEMA_VERSION = "outcomeci.run-manifest/v1"


def build_manifest(
    *,
    outcome_root: Path,
    artifact_base: Path,
    run_id: str,
    workflow_run_id: str | None,
    trajectory_version: int | None,
    step: str,
    workflow_revision: str,
    backend_provider: str,
    state_repository: str | None,
    context_provider: str,
    context_revision_id: str | None,
    repository_base_commits: dict[str, str | None],
    runner: str,
    model: str | None,
    transcript: dict[str, Any],
    step_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical envelope without backend-specific omissions."""
    artifacts = sorted(
        str(path.relative_to(artifact_base))
        for path in outcome_root.rglob("*")
        if path.is_file()
        and path.name not in {"manifest.json", "run.json"}
        and not private_path(path.relative_to(outcome_root))
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "outcome_run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "trajectory_version": trajectory_version,
        "step": step,
        "workflow_revision": workflow_revision,
        "backend": {"provider": backend_provider, "state_repository": state_repository},
        "context": {"provider": context_provider, "revision_id": context_revision_id},
        "repository_base_commits": repository_base_commits,
        "runner": {"provider": runner, "model": model or "provider-default"},
        "step_contract": step_contract,
        "transcript": transcript,
        "artifacts": artifacts,
    }
