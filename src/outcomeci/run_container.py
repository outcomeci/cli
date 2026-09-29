"""Run one `oci workflow run` job inside the runner container.

The host pipes a bundle (the workflow file, trigger payload, secret values and
agent logins) on stdin. It mounts the workflow directory read-only at /src and
a private output directory at /oci-run. The run works on a copy of /src under
/oci-run/work, with HOME at /oci-run/home, through the same execution path as a
cloud run, isolated by the container itself. The host reads the result, the
run's state and a rotated Codex login from /oci-run once the container ends.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cloud_runner.main import _inject_agent_credential
from .cloud_runner.models import ContractError
from .config import ConfigError, compile_workflow
from .process import ExecutionError
from .security import atomic_write_json

# The checkout is mounted read-only at CONTAINER_SOURCE and copied into the
# private output mount, where the run's work dir and HOME live: the agent runs
# with its own sandbox off (the container is the boundary), so it never gets
# write access to the host checkout, and the host can read the run's state and
# a rotated Codex login back however the container ended.
CONTAINER_SOURCE = "/src"
CONTAINER_OUTPUT = "/oci-run"
OUTPUT_WORK = "work"
OUTPUT_HOME = "home"
BUNDLE_KEYS = ("config", "trigger", "payload", "values", "expires_at", "credentials")


def _lease_resolver(values: dict[str, Any], expires_at: str):
    expires = datetime.fromisoformat(expires_at)

    def resolver(reference: str) -> Any:
        if datetime.now(UTC) >= expires:
            raise ExecutionError("the run's credential lease expired; run the command again")
        if not reference.startswith("vault:"):
            raise ExecutionError("cloud credentials must use vault references")
        path = reference.removeprefix("vault:")
        if path not in values:
            raise ExecutionError(
                f"credential {path!r} is not granted to this workflow; "
                "grant it with `oci vault grant`"
            )
        return values[path]

    return resolver


def _continue(
    root: Path,
    config: Path,
    compiled: dict[str, Any],
    result: dict[str, Any],
    options: Any,
    *,
    auto_continue: bool,
) -> dict[str, Any]:
    """With auto_continue, drive each ready step in turn."""
    from . import local

    phase_count = len(compiled["instructions"]["phases"]) if auto_continue else 0
    while auto_continue and len(result.get("completed_phases", [])) != phase_count:
        if not result.get("ready_phases") or result.get("status") == "completed":
            break
        next_phase = result["ready_phases"][0]
        print(f"Continuing into step {next_phase!r}...", file=sys.stderr)
        result = local.continue_run(root, config, result["run_id"], approve=True, options=options)
    return result


def resume(
    root: Path,
    config: Path,
    compiled: dict[str, Any],
    run_id: str,
    options: Any,
    *,
    auto_continue: bool,
) -> dict[str, Any]:
    """Retry a run that stopped on an error or was interrupted, from its recorded state.

    A run is owned by the one container that runs it, so a run still marked
    running here was interrupted, and is recorded as such before the retry."""
    from . import local

    state = local._read(root, run_id)
    if state.get("status") == "running":
        state.update({"status": "error", "error": "the run was interrupted"})
        local._write(root, state)
    result = local.retry(root, config, run_id, options=options)
    return _continue(root, config, compiled, result, options, auto_continue=auto_continue)


def execute(
    root: Path,
    config: Path,
    compiled: dict[str, Any],
    name: str,
    payload: dict[str, Any],
    options: Any,
    *,
    auto_continue: bool,
) -> dict[str, Any]:
    """Trigger the run and, with auto_continue, drive each ready step in turn."""
    from . import local

    result = local.trigger(root, config, name, payload, options=options)
    return _continue(root, config, compiled, result, options, auto_continue=auto_continue)


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
        print("oci: invalid run bundle on stdin", file=sys.stderr)
        return 2
    try:
        return run_bundle(bundle, source=Path(CONTAINER_SOURCE), output=Path(CONTAINER_OUTPUT))
    except KeyboardInterrupt:
        # The host stops the container this way when it is interrupted.
        print("oci: run interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
