from __future__ import annotations

import json
from pathlib import Path

import pytest

from outcomeci.locking import verify_lock, write_lock
from outcomeci.process import ExecutionError
from outcomeci.repository import initialize


def test_lock_is_reproducible_and_contains_no_credentials(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    config, output = tmp_path / "outcome.yml", tmp_path / "outcome.lock"
    first = write_lock(config, output)
    second = write_lock(config, output)
    assert first == second
    assert verify_lock(config, output)["valid"] is True
    serialized = output.read_text()
    assert "credential" not in serialized.lower()
    assert json.loads(serialized)["schema"].startswith("https://outcomeci.com/schemas/")


def test_lock_detects_workflow_drift(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    config, output = tmp_path / "outcome.yml", tmp_path / "outcome.lock"
    write_lock(config, output)
    instruction = tmp_path / ".outcomeci/instructions/intake.md"
    instruction.write_text(instruction.read_text() + "\nNew policy.\n")
    with pytest.raises(ExecutionError, match="does not match"):
        verify_lock(config, output)
