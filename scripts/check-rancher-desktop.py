#!/usr/bin/env python3
"""Probe runner mounts and a credential-free workflow on Rancher Desktop.

Usage: python3 scripts/check-rancher-desktop.py ghcr.io/outcomeci/outcome-runner:VERSION
Requires Rancher Desktop running with the Moby engine. Uses its explicit Docker
context without changing the user's current context. Requires the oci CLI on PATH.
No credentials are used; the workflow skips its model step.
"""

import argparse
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

PROBE = r"""
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import outcomeci.runtime.container

assert json.load(sys.stdin) == {"probe": "stdin"}
assert Path("/src/input.txt").read_text() == "source unchanged"
try:
    Path("/src/input.txt").write_text("unexpected write")
except OSError as exc:
    assert exc.errno == errno.EROFS, exc
else:
    raise AssertionError("source mount is writable")
home = Path(os.environ["HOME"])
home.mkdir(mode=0o700)
shutil.copytree("/src", "/oci-run/work")
Path("/oci-run/work/input.txt").write_text("private copy")
for executable in ("git", "codex", "claude", "opencode"):
    assert shutil.which(executable), executable
Path("/oci-run/result.json").write_text(json.dumps({"uid": os.getuid(), "ok": True}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Exact runner image tag or digest to test")
    args = parser.parse_args()
    if not hasattr(os, "getuid"):
        parser.error("Run from macOS, Linux, or a Rancher-integrated WSL distribution")
    docker = ["docker", "--context", "rancher-desktop"]
    settings = json.loads(
        subprocess.run(
            ["rdctl", "list-settings"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if settings["containerEngine"]["name"] != "moby":
        parser.error(
            "Select dockerd (moby) in Rancher Desktop; containerd/nerdctl is not supported"
        )
    subprocess.run(
        [*docker, "info", "--format", "{{.Name}} / {{.ServerVersion}} / {{.Architecture}}"],
        check=True,
    )
    subprocess.run(["rdctl", "version"], check=True)
    # A home-directory mount also exercises Desktop's host file sharing.
    with tempfile.TemporaryDirectory(prefix="oci-rancher-", dir=Path.home()) as temp:
        root = Path(temp)
        source, output = root / "source", root / "output"
        source.mkdir()
        output.mkdir()
        (source / "input.txt").write_text("source unchanged")
        name = f"oci-rancher-probe-{uuid.uuid4().hex[:12]}"
        try:
            subprocess.run(
                [
                    *docker,
                    "run",
                    "--rm",
                    "-i",
                    "--init",
                    "--name",
                    name,
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-e",
                    "HOME=/oci-run/home",
                    "-e",
                    "USER=oci",
                    "-e",
                    "LOGNAME=oci",
                    "--mount",
                    f"type=bind,src={source},dst=/src,readonly",
                    "--mount",
                    f"type=bind,src={output},dst=/oci-run",
                    "-w",
                    "/oci-run",
                    "--entrypoint",
                    "/opt/oci/bin/python",
                    args.image,
                    "-c",
                    PROBE,
                ],
                input=json.dumps({"probe": "stdin"}),
                text=True,
                check=True,
                timeout=300,
            )
            assert json.loads((output / "result.json").read_text()) == {
                "uid": os.getuid(),
                "ok": True,
            }
            assert (source / "input.txt").read_text() == "source unchanged"
            assert (output / "work/input.txt").read_text() == "private copy"
        finally:
            subprocess.run([*docker, "rm", "--force", name], capture_output=True)
        (source / "outcome.yml").write_text("""apiVersion: outcomeci.workflow/v1
name: rancher-smoke
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
        (source / "smoke.md").write_text("Return ok true.")
        completed = subprocess.run(
            [
                "oci",
                "workflow",
                "run",
                "--dir",
                str(source),
                "--image",
                args.image,
                "--runtime",
                "docker",
            ],
            env={**os.environ, "DOCKER_CONTEXT": "rancher-desktop"},
            text=True,
            stdout=subprocess.PIPE,
            check=True,
            timeout=300,
        )
        result = json.loads(completed.stdout)
        assert result["status"] == "completed"
        assert result["skipped_steps"] == ["smoke"]
        record = source / ".outcomeci" / "outcomes" / result["run_id"] / "run.json"
        assert json.loads(record.read_text())["status"] == "completed"
    subprocess.run(
        [*docker, "image", "inspect", args.image, "--format", "{{.Id}} {{json .RepoDigests}}"],
        check=True,
    )
    print("PASS: runner import, stdin, init, UID, read-only source, writable HOME and output.")
    print("PASS: CLI workflow startup, DNS, and run-record writeback.")
    print("Model inference and agent authentication are not exercised.")


if __name__ == "__main__":
    main()
