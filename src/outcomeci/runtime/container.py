"""Run one `oci workflow run` job inside the runner container.

The host pipes a bundle (the workflow file, trigger payload, secret values and
agent logins) on stdin. It mounts the workflow directory read-only at /src and
a private output directory at /oci-run. The run works on a copy of /src under
/oci-run/work, with HOME at /oci-run/home, through the same execution path as a
cloud run, isolated by the container itself. The host reads the result, the
run's state, a rotated Codex login and any rotated Vault secrets from /oci-run
once the container ends.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from outcomeci.cloud_runner.main import _inject_agent_credential
from outcomeci.cloud_runner.models import ContractError
from outcomeci.runtime.process import ExecutionError
from outcomeci.security import atomic_write_json
from outcomeci.vault.leases import LeaseResolver
from outcomeci.workflow.compiler import ConfigError, compile_workflow

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


ROTATIONS = "rotations.json"


def _lease_resolver(values: dict[str, Any], expires_at: str, output: Path | None = None):
    """The bundle's Vault values. A rotated secret is recorded in the output
    mount the moment it arrives, where the host saves it to the Vault the
    values came from, however the run ends."""

    def record(path: str, secrets: dict[str, str]) -> None:
        assert output is not None
        file = output / ROTATIONS
        rotations = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
        rotations[path] = {**rotations.get(path, {}), **secrets}
        atomic_write_json(file, rotations, mode=0o600)

    return LeaseResolver(values, expires_at, on_rotate=record if output is not None else None)


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
    from outcomeci.runtime import engine as local

    step_count = len(compiled["instructions"]["steps"]) if auto_continue else 0
    while auto_continue and len(result.get("completed_steps", [])) != step_count:
        if not result.get("ready_steps") or result.get("status") == "completed":
            break
        next_step = result["ready_steps"][0]
        print(f"Continuing into step {next_step!r}...", file=sys.stderr)
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
    from outcomeci.runtime import engine as local

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
    from outcomeci.runtime import engine as local

    result = local.trigger(root, config, name, payload, options=options)
    return _continue(root, config, compiled, result, options, auto_continue=auto_continue)


def run_bundle(bundle: dict[str, Any], *, source: Path, output: Path) -> int:
    from outcomeci.runtime import engine as local

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
            credential_resolver=_lease_resolver(bundle["values"], bundle["expires_at"], output),
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
