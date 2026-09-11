"""Shared artifact manifest contract for local and managed outcome runs."""
from __future__ import annotations

from pathlib import Path
from typing import Any


SCHEMA_VERSION = "outcomeci.outcome-manifest/v1alpha1"


def build_manifest(
    *,
    outcome_root: Path,
    artifact_base: Path,
    run_id: str,
    workflow_run_id: str | None,
    trajectory_version: int | None,
    phase: str,
    workflow_revision: str,
    backend_provider: str,
    state_repository: str | None,
    context_provider: str,
    context_revision_id: str | None,
    constitution_sha256: str,
    repository_base_commits: dict[str, str | None],
    runner: str,
    model: str | None,
    transcript: dict[str, Any],
    phase_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical envelope without backend-specific omissions."""
    artifacts = sorted(
        str(path.relative_to(artifact_base))
        for path in outcome_root.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "run.json"}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "odl_run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "trajectory_version": trajectory_version,
        "phase": phase,
        "workflow_revision": workflow_revision,
        "backend": {"provider": backend_provider, "state_repository": state_repository},
        "context": {"provider": context_provider, "revision_id": context_revision_id},
        "standup": str((outcome_root / "standup.md").relative_to(artifact_base)),
        "constitution_sha256": constitution_sha256,
        "repository_base_commits": repository_base_commits,
        "runner": {"provider": runner, "model": model or "provider-default"},
        "phase_contract": phase_contract,
        "transcript": transcript,
        "artifacts": artifacts,
    }
