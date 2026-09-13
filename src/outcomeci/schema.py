"""Packaged, versioned workflow schemas."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

CURRENT_SCHEMA = "outcome-v1alpha1.schema.json"


def schema_path() -> Path:
    return Path(str(files("outcomeci").joinpath("schemas", CURRENT_SCHEMA)))


def load_schema() -> dict[str, Any]:
    return json.loads(schema_path().read_text(encoding="utf-8"))


def export_schema(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(load_schema(), indent=2) + "\n", encoding="utf-8")
    return output
