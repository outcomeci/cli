"""Portable conformance checks for workflow runtimes."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .config import compile_workflow
from .integrations import IntegrationExecutor
from .locking import build_lock
from .repository import initialize
from .schema import load_schema


def run(workflow: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="outcomeci-conformance-") as temporary:
        root = Path(temporary)
        if workflow is None:
            initialize(root, "filesystem")
            workflow = root / "outcome.yml"
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        jsonschema.validate(document, load_schema())
        compiled = compile_workflow(workflow)
        executor = IntegrationExecutor(compiled)
        phases = compiled["instructions"]["phases"]
        dry_runs = [executor.dry_run(phase) for phase in phases]
        lock = build_lock(workflow)
        checks = {
            "schema": True,
            "compile": True,
            "phase_discovery": len(dry_runs) == len(phases),
            "credential_blind_dry_run": all(
                not result["credentials_resolved"] and not result["requests_executed"]
                for result in dry_runs
            ),
            "reproducible_lock": lock["workflow_revision"] == compiled["workflow_revision"],
        }
        return {
            "conformant": all(checks.values()),
            "contract": "outcomeci.runner/v1alpha1",
            "workflow_revision": compiled["workflow_revision"],
            "checks": checks,
        }
