"""Compile lowered phase-graph fixtures, the shape every workflow compiles to.

Executor tests describe integrations directly in this shape, so they cover
auth types and access modes no connector exposes yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from outcomeci.config import compile_lowered


def compile_file(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return compile_lowered(document, path)
