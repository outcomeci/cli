"""Real Docker/Podman host checks; opt in with OUTCOMECI_TEST_IMAGE.

Run on an SELinux-enforcing host to exercise labeling and rootless ownership.
No model call, external side effect, or agent credential is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from outcomeci.runtime.container_engine import discover_engine
from outcomeci.runtime.launcher import run_local

IMAGE = os.environ.get("OUTCOMECI_TEST_IMAGE")
RUNTIME = os.environ.get("OUTCOMECI_TEST_RUNTIME")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set OUTCOMECI_TEST_IMAGE for real containers")


def _label(path):
    try:
        return os.getxattr(path, "security.selinux")
    except OSError:
        return None


def test_private_mounts_are_readable_output_is_host_owned_and_source_stays_readonly(
    tmp_path, request
):
    engine = discover_engine(RUNTIME)
    scratch = engine.temporary_directory(prefix="oci-test-")
    request.addfinalizer(scratch.cleanup)
    source, output = tmp_path / "checkout", Path(scratch.name)
    source.mkdir(mode=0o700)
    marker = source / "marker"
    marker.write_text("original")
    original_label = _label(marker)
    probe = """
from pathlib import Path
assert Path('/src/marker').read_text() == 'original'
try:
    Path('/src/marker').write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('source was writable')
Path('/oci-run/result').write_text('success')
"""
    with engine.mounts(source, output) as mounts:
        subprocess.run(
            [
                engine.command,
                "run",
                "--rm",
                *engine.user_args(),
                *mounts,
                "--entrypoint",
                "/opt/oci/bin/python",
                IMAGE,
                "-c",
                probe,
            ],
            check=True,
            timeout=120,
        )
    result = output / "result"
    assert result.read_text() == "success"
    assert (result.stat().st_uid, result.stat().st_gid) == (os.getuid(), os.getgid())
    assert marker.read_text() == "original"
    assert _label(marker) == original_label


def test_workflow_runs_and_returns_its_record_without_credentials(tmp_path):
    config = tmp_path / "outcome.yml"
    config.write_text("""apiVersion: outcomeci.workflow/v1
name: container-smoke
trigger: manual
reasoning:
  light: {model: openai/gpt-4.1-mini}
steps:
  - smoke:
      when: trigger.run_model == true
      using: light
      with: trigger
      reason: smoke.md
      returns: {ok: boolean}
""")
    (tmp_path / "smoke.md").write_text("Return ok true.")
    original_label = _label(config)
    result = run_local(
        tmp_path,
        config,
        image=IMAGE,
        runtime=RUNTIME,
        network=os.environ.get("OUTCOMECI_TEST_NETWORK"),
    )
    assert result["status"] == "completed"
    assert result["skipped_steps"] == ["smoke"]
    record = tmp_path / ".outcomeci" / "outcomes" / result["run_id"] / "run.json"
    assert json.loads(record.read_text())["status"] == "completed"
    assert record.stat().st_uid == os.getuid()
    assert _label(config) == original_label
