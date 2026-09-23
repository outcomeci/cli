from __future__ import annotations

import json
import stat
from pathlib import Path

from outcomeci.security import atomic_write_json, atomic_write_text


def test_atomic_write_text_creates_parents_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "value.txt"

    atomic_write_text(path, "hello\n")

    assert path.read_text(encoding="utf-8") == "hello\n"
    assert list(path.parent.iterdir()) == [path]


def test_atomic_write_text_replaces_existing_content_in_one_step(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_text("stale", encoding="utf-8")

    atomic_write_text(path, "fresh")

    assert path.read_text(encoding="utf-8") == "fresh"


def test_atomic_write_text_applies_mode_before_the_rename(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"

    atomic_write_text(path, "s3cr3t", mode=stat.S_IRUSR | stat.S_IWUSR)

    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


def test_atomic_write_json_writes_indented_sorted_json_with_trailing_newline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "value.json"

    atomic_write_json(path, {"b": 1, "a": 2})

    text = path.read_text(encoding="utf-8")
    assert text == '{\n  "a": 2,\n  "b": 1\n}\n'
    assert json.loads(text) == {"a": 2, "b": 1}


def test_atomic_write_json_can_preserve_key_order(tmp_path: Path) -> None:
    path = tmp_path / "value.json"

    atomic_write_json(path, {"b": 1, "a": 2}, sort_keys=False)

    assert list(json.loads(path.read_text(encoding="utf-8")).keys()) == ["b", "a"]
