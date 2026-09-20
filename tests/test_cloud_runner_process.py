from __future__ import annotations

import os
import sys
from pathlib import Path

from outcomeci.cloud_runner.process import PTY_COLUMNS, PTY_ROWS, run


def test_terminal_pty_gets_a_wide_winsize_so_boxed_output_does_not_wrap(tmp_path: Path) -> None:
    """A 0x0 pty makes terminal UIs wrap/clip long output (e.g. a boxed
    credential display), silently truncating whatever we later regex out of
    the transcript. The child must see an explicit, wide window instead."""
    script = "import os,sys; size=os.get_terminal_size(sys.stdout.fileno()); print(size.columns, size.lines)"
    result = run(
        (sys.executable, "-c", script),
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=10,
        terminal=True,
    )
    assert result.returncode == 0
    columns, lines = (int(value) for value in result.stdout.strip().split())
    assert columns == PTY_COLUMNS
    assert lines == PTY_ROWS
