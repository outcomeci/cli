"""Packaged, versioned workflow schemas."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .contracts import CONTRACT_FILES

CURRENT_SCHEMA = "outcome-v1alpha1.schema.json"


def schema_path(name: str = "workflow") -> Path:
    filename = CURRENT_SCHEMA if name == "workflow" else CONTRACT_FILES[name]
    return Path(str(files("outcomeci").joinpath("schemas", filename)))


def load_schema(name: str = "workflow") -> dict[str, Any]:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def export_schema(output: Path, name: str = "workflow") -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(load_schema(name), indent=2) + "\n", encoding="utf-8")
    return output
