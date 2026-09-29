"""Run one `oci workflow debug --image` job inside the runner container.

The host pipes a bundle (the workflow file, trigger payload, leased vault
values and the leased agent credential) on stdin. It mounts the workflow
directory read-only at /src and a private output directory at /debug-out.
The run works on a copy of /src under /debug-out/work, with HOME at
/debug-out/home, through the same execution path as a cloud run, isolated by
the container itself. The host reads the result, the run's state and a
rotated Codex login from /debug-out once the container ends.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .cloud_runner.main import _inject_agent_credential
from .cloud_runner.models import ContractError
from .config import ConfigError, compile_workflow
from .debug import (
    CONTAINER_OUTPUT,
    CONTAINER_SOURCE,
    OUTPUT_HOME,
    OUTPUT_WORK,
    _lease_resolver,
    execute,
    resume,
)
from .process import ExecutionError
from .security import atomic_write_json

BUNDLE_KEYS = ("config", "trigger", "payload", "values", "expires_at", "credentials")


def run_bundle(bundle: dict[str, Any], *, source: Path, output: Path) -> int:
    from . import local

    work = output / OUTPUT_WORK
    home = output / OUTPUT_HOME
    shutil.copytree(source, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        for login in bundle["credentials"]:
            os.environ.update(
                _inject_agent_credential(home, login["provider"], login["credential"])
            )
        config = work / bundle["config"]
        options = local.ExecutionOptions(
            agent=bundle.get("agent"),
            model=bundle.get("model"),
            credential_resolver=_lease_resolver(bundle["values"], bundle["expires_at"]),
            _container_isolated=True,
        )
        compiled = compile_workflow(config)
        auto_continue = bool(bundle.get("auto_continue"))
        result = (
            resume(work, config, compiled, bundle["retry"], options, auto_continue=auto_continue)
            if bundle.get("retry")
            else execute(
                work,
                config,
                compiled,
                bundle["trigger"],
                bundle["payload"],
                options,
                auto_continue=auto_continue,
            )
        )
    except (ConfigError, ContractError, ExecutionError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1
    # The host prints the result; stdout here is shared with the host's.
    atomic_write_json(output / "result.json", result, mode=0o600)
    return 0


def main() -> int:
    try:
        bundle = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        bundle = None
    if not isinstance(bundle, dict) or any(key not in bundle for key in BUNDLE_KEYS):
        print("oci: invalid debug bundle on stdin", file=sys.stderr)
        return 2
    try:
        return run_bundle(bundle, source=Path(CONTAINER_SOURCE), output=Path(CONTAINER_OUTPUT))
    except KeyboardInterrupt:
        # The host stops the container this way when it is interrupted.
        print("oci: debug run interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
