"""Reproducible workflow resolution through outcome.lock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import compile_workflow
from .process import ExecutionError
from .schema import load_schema


def build_lock(config: Path) -> dict[str, Any]:
    compiled = compile_workflow(config)
    phases = compiled["instructions"]["phases"]
    operations = {
        capability: {
            "policy": definition["policy"],
            "input": definition["input"],
            "response": definition["response"],
        }
        for integration, value in compiled["workflow"]["spec"].get("integrations", {}).items()
        for capability, definition in (
            (f"{integration}.{operation}", definition)
            for operation, definition in value["operations"].items()
        )
    }
    return {
        "lock_version": 1,
        "schema": load_schema()["$id"],
        "workflow_revision": compiled["workflow_revision"],
        "engine": {
            "contract": compiled["engine_version"],
            "package": compiled["engine_package_version"],
        },
        "integration_packages": compiled["workflow"]["spec"].get("integration_packages", []),
        "phases": {
            name: {
                "runner": value["policy"].get("runner"),
                "model": value["policy"].get("model"),
                "capabilities": value["capabilities"],
            }
            for name, value in phases.items()
        },
        "operations": operations,
    }


def write_lock(config: Path, output: Path) -> dict[str, Any]:
    value = build_lock(config)
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def verify_lock(config: Path, lock_path: Path) -> dict[str, Any]:
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"could not read outcome lock: {exc}") from exc
    expected = build_lock(config)
    if existing != expected:
        raise ExecutionError(
            "outcome.lock does not match the workflow; run `oci outcome lock` to update it"
        )
    return {
        "valid": True,
        "workflow_revision": expected["workflow_revision"],
        "lock": str(lock_path),
    }
