"""Run a backend: outcomeci workflow locally against real cloud vault credentials.

`run_local` runs a workflow the same way in the runner image with no cloud at
all: secrets come from the checkout's local Vault and the agent login from
this machine.

For debugging a cloud run without waiting on a merge-build-deploy cycle: this
issues a short-lived vault lease scoped to one workflow (gated server-side on
the same permission as managing that workflow's vault grants) and executes it
through the same local.trigger() path the ECS runner itself uses, with full,
unredacted output in this terminal.

With an image, the run executes inside that runner container instead of on
this host, with the workspace's cloud agent credential leased the same way a
cloud run receives it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cloud import (
    CloudRequestError,
    complete_debug_agent_lease,
    complete_debug_lease,
    credentials_path,
    issue_debug_lease,
    renew_debug_agent_lease,
)
from .config import compile_workflow
from .process import ExecutionError
from .security import atomic_write_json

# Container layout. The checkout is mounted read-only at CONTAINER_SOURCE and
# copied into the private output mount, where the run's work dir and HOME live:
# the agent runs with its own sandbox off (the container is the boundary), so
# it never gets write access to the host checkout, and the host can read the
# run's state and a rotated Codex login back however the container ended.
CONTAINER_SOURCE = "/src"
CONTAINER_OUTPUT = "/debug-out"
OUTPUT_WORK = "work"
OUTPUT_HOME = "home"
IMAGE_LEASE_TTL_SECONDS = 3600
AGENT_RENEW_INTERVAL_SECONDS = 300
AGENT_RENEW_TTL_SECONDS = 900
CONTAINER_STOP_GRACE_SECONDS = 30
RELEASE_ATTEMPTS = 3
RUN_STATE_DIRS = ("outcomes", ".broker")
RUNNER_IMAGE = "ghcr.io/outcomeci/outcome-runner"
LOCAL_RUN_TTL_SECONDS = 12 * 3600
# Agent logins a local run reads from the environment, or else from the local
# Vault at agents/<provider>. Codex keeps its own login file instead.
AGENT_ENV = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "opencode": "OPENROUTER_API_KEY"}
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _lease_resolver(values: dict[str, Any], expires_at: str):
    expires = datetime.fromisoformat(expires_at)

    def resolver(reference: str) -> Any:
        if datetime.now(UTC) >= expires:
            raise ExecutionError("debug credential lease expired; run the command again")
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


def _synthesize_payload(trigger_name: str, definition: dict[str, Any]) -> dict[str, Any]:
    trigger_type = definition["type"]
    if trigger_type == "manual":
        return {}
    if trigger_type == "cron":
        return {
            "schema_version": "outcomeci.trigger.cron/v1",
            "type": "cron",
            "schedule_id": str(uuid.uuid4()),
            "generation": 1,
            "schedule_arn": "arn:debug:local:schedule",
            "scheduled_at": datetime.now(UTC).isoformat(),
            "execution_id": f"debug-{uuid.uuid4()}",
            "attempt_number": 1,
            "trigger_name": trigger_name,
        }
    raise ExecutionError(
        f"cannot synthesize a payload for trigger type {trigger_type!r}; pass --payload"
    )


def _default_agent(compiled: dict[str, Any]) -> str:
    """The provider a cloud claim would lease for this workflow."""
    agents = (compiled.get("workflow") or {}).get("spec", {}).get("agents") or {}
    return (agents.get("default") or {}).get("runner") or "codex"


def _runners(compiled: dict[str, Any], agent: str | None) -> list[str]:
    """Every agent provider the run needs a login for: the default first, then any
    runner a step selects for itself, or only `agent` when it overrides them all."""
    if agent:
        return [agent]
    found = [_default_agent(compiled)]
    for phase in (compiled.get("instructions") or {}).get("phases", {}).values():
        runner = (phase.get("policy") or {}).get("runner")
        if runner and runner not in found:
            found.append(runner)
    return found


def _continue(
    root: Path,
    config: Path,
    compiled: dict[str, Any],
    result: dict[str, Any],
    options: Any,
    *,
    auto_continue: bool,
) -> dict[str, Any]:
    """With auto_continue, drive each ready phase in turn."""
    from . import local

    phase_count = len(compiled["instructions"]["phases"]) if auto_continue else 0
    while auto_continue and len(result.get("completed_phases", [])) != phase_count:
        if not result.get("ready_phases") or result.get("status") == "completed":
            break
        next_phase = result["ready_phases"][0]
        print(f"Continuing into phase {next_phase!r}...", file=sys.stderr)
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

    A debug run is owned by this one process, so a run still marked running
    here was interrupted, and is recorded as such before the retry."""
    from . import local

    state = local._read(root, run_id)
    if state.get("status") == "running":
        state.update({"status": "error", "error": "the debug run was interrupted"})
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
    """Trigger the run and, with auto_continue, drive each ready phase in turn."""
    from . import local

    result = local.trigger(root, config, name, payload, options=options)
    return _continue(root, config, compiled, result, options, auto_continue=auto_continue)


def _resolve_trigger(
    compiled: dict[str, Any], trigger_name: str | None, payload_path: Path | None
) -> tuple[str, dict[str, Any]]:
    if not trigger_name:
        raise ExecutionError("pass --trigger <name>, or --run <invocation-id> to replay one")
    definition = compiled["triggers"].get(trigger_name)
    if definition is None:
        raise ExecutionError(f"workflow does not declare trigger {trigger_name}")
    payload = (
        json.loads(payload_path.read_text(encoding="utf-8"))
        if payload_path is not None
        else _synthesize_payload(trigger_name, definition)
    )
    return trigger_name, payload


def run(
    root: Path,
    config: Path,
    workspace_id: str,
    workflow_id: str,
    *,
    trigger_name: str | None = None,
    invocation_id: str | None = None,
    payload_path: Path | None = None,
    agent: str | None = None,
    model: str | None = None,
    auto_continue: bool = False,
    image: str | None = None,
    network: str | None = None,
    retry_run: str | None = None,
) -> dict[str, Any]:
    from . import local

    compiled = compile_workflow(config)
    # Everything that can fail on the user's input fails here, before a lease
    # takes the workspace's agent connection away from its cloud runs.
    if network is not None and image is None:
        raise ExecutionError("--network applies only to --image runs")
    if retry_run is not None and invocation_id is not None:
        raise ExecutionError("choose --retry or --run, not both")
    if image is not None:
        root, config = root.resolve(), config.resolve()
        if not config.is_relative_to(root):
            raise ExecutionError("the workflow file must live inside --dir to run in an image")
        _check_image(image)
        _flush_pending_releases()
    if retry_run is not None:
        _retryable(root, retry_run)
        name, payload = None, None
    else:
        name, payload = (
            _resolve_trigger(compiled, trigger_name, payload_path)
            if invocation_id is None
            else (None, None)
        )
    leases = (
        _issue_leases(workspace_id, workflow_id, _runners(compiled, agent), invocation_id)
        if image is not None
        else [issue_debug_lease(workspace_id, workflow_id, invocation_id=invocation_id)]
    )
    lease = leases[0]

    with ExitStack() as held:
        try:
            if image is not None:
                held.enter_context(_termination_interrupts())
            hold = (
                held.enter_context(_hold_agent_lease(workspace_id, workflow_id, leases))
                if image is not None
                else None
            )
            if invocation_id is not None:
                name = lease.get("trigger_name")
                if not name:
                    raise ExecutionError(
                        "this invocation has no recorded trigger; it may predate this contract"
                    )
                payload = lease.get("input") or {}
            where = f"inside {image}" if image is not None else "locally"
            what = f"run {retry_run}" if retry_run else f"trigger {name!r}"
            print(
                f"Debugging {what} {where} against workspace {workspace_id}, "
                f"workflow {workflow_id}, using real cloud vault credentials.",
                file=sys.stderr,
            )
            if hold is not None:
                result = _run_in_image(
                    image,
                    root,
                    config,
                    lease,
                    hold,
                    name=name,
                    payload=payload,
                    agent=agent,
                    model=model,
                    auto_continue=auto_continue,
                    network=network,
                    retry_run=retry_run,
                )
            else:
                options = local.ExecutionOptions(
                    agent=agent,
                    model=model,
                    credential_resolver=_lease_resolver(lease["values"], lease["expires_at"]),
                    _container_isolated=False,
                )
                result = (
                    resume(root, config, compiled, retry_run, options, auto_continue=auto_continue)
                    if retry_run
                    else execute(
                        root, config, compiled, name, payload, options, auto_continue=auto_continue
                    )
                )
        except BaseException:
            if invocation_id is not None:
                with suppress(ExecutionError):
                    complete_debug_lease(workspace_id, workflow_id, invocation_id, "failed")
            raise
    if invocation_id is not None:
        complete_debug_lease(workspace_id, workflow_id, invocation_id, "completed")
    return result


def _retryable(root: Path, run_id: str) -> None:
    if not _RUN_ID.match(run_id):
        raise ExecutionError(f"{run_id!r} is not a run id")
    record = root / ".outcomeci" / "outcomes" / run_id / "run.json"
    try:
        state = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"no recorded run {run_id} in {root}") from exc
    if state.get("status") not in {"error", "running"}:
        raise ExecutionError(
            f"run {run_id} is {state.get('status')}; only a failed or interrupted run retries"
        )


def _issue_leases(
    workspace_id: str, workflow_id: str, runners: list[str], invocation_id: str | None
) -> list[dict[str, Any]]:
    """One debug lease per runner the run needs, each holding that runner's login.

    Only the first claims a replayed invocation. When a later one is refused,
    the logins already leased are released before the refusal is raised.
    """
    leases: list[dict[str, Any]] = []
    try:
        for index, runner in enumerate(runners):
            leases.append(
                issue_debug_lease(
                    workspace_id,
                    workflow_id,
                    invocation_id=invocation_id if index == 0 else None,
                    ttl_seconds=IMAGE_LEASE_TTL_SECONDS,
                    agent_provider=runner,
                )
            )
    except BaseException:
        for lease in leases:
            if isinstance(lease.get("agent"), dict):
                _release_agent_lease(workspace_id, workflow_id, lease["agent"], "failed", None)
        raise
    return leases


@contextmanager
def _termination_interrupts() -> Iterator[None]:
    """Treat SIGTERM and SIGHUP like Ctrl-C while an image run holds a lease.

    Their default action ends this process on the spot, leaving the container
    running and the lease held; as an interrupt they stop the container and
    release the lease first.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _check_image(image: str) -> None:
    """Fail before any lease when the image cannot run a debug job at all."""
    try:
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/opt/oci/bin/python",
                image,
                "-c",
                "import outcomeci.debug_container",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ExecutionError("docker is not installed or not on PATH") from exc
    if probe.returncode != 0:
        detail = (probe.stderr.strip().splitlines() or ["no output"])[-1]
        raise ExecutionError(
            f"{image} cannot run debug jobs; it needs an OutcomeCI runner build "
            f"that includes outcomeci.debug_container ({detail})"
        )


@dataclass
class _AgentHold:
    """The agent leases held for one image run, and where that run keeps its files."""

    agent_leases: list[dict[str, Any]]
    output: Path
    status: str = "failed"


@contextmanager
def _hold_agent_lease(
    workspace_id: str, workflow_id: str, leases: list[dict[str, Any]]
) -> Iterator[_AgentHold]:
    """Hold every agent lease from issue to release, whatever happens in between.

    Each lease is renewed while held and always released afterwards. A Codex
    login rotated during the run is always written back: once Codex spends the
    old refresh token, the rotated one is the only valid copy.
    """
    agent_leases = [lease.get("agent") for lease in leases]
    if not agent_leases or not all(isinstance(item, dict) for item in agent_leases):
        raise ExecutionError("the debug lease carried no agent credential for the image")
    with tempfile.TemporaryDirectory(prefix="oci-debug-") as output:
        os.chmod(output, 0o700)
        hold = _AgentHold(agent_leases, Path(output))
        try:
            with ExitStack() as beats:
                for agent_lease in agent_leases:
                    beats.enter_context(
                        _agent_lease_heartbeat(workspace_id, workflow_id, agent_lease)
                    )
                yield hold
        finally:
            for agent_lease in agent_leases:
                _release_agent_lease(
                    workspace_id,
                    workflow_id,
                    agent_lease,
                    hold.status,
                    _codex_rotation(hold.output / OUTPUT_HOME, agent_lease),
                )


def _codex_rotation(home: Path, agent_lease: dict[str, Any]) -> dict[str, Any] | None:
    """The Codex login the run left behind, when it differs from the leased one."""
    if agent_lease["provider"] != "codex":
        return None
    try:
        login = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(login, dict) or login == agent_lease["credential"]:
        return None
    return login


def _pending_releases_dir() -> Path:
    return credentials_path().parent / "debug-releases"


def _release_agent_lease(
    workspace_id: str,
    workflow_id: str,
    agent_lease: dict[str, Any],
    status_value: str,
    writeback: dict[str, Any] | None,
) -> None:
    """Release the lease, keeping a rotated login on disk if the release can't land.

    Never raises, so a failed release does not mask how the run itself ended.
    """
    release = {
        "workspace_id": workspace_id,
        "workflow_id": workflow_id,
        "job_id": str(agent_lease["job_id"]),
        "token": agent_lease["token"],
        "status_value": status_value,
        "agent_credential": writeback,
        "expected_credential_version": (
            agent_lease["credential_version"] if writeback is not None else None
        ),
    }
    for attempt in range(RELEASE_ATTEMPTS):
        try:
            _send_release(release)
            return
        except CloudRequestError as exc:
            if not exc.transient:
                print(f"oci: warning: {exc}", file=sys.stderr)
                return
            error: ExecutionError = exc
        except ExecutionError as exc:
            error = exc
        if attempt + 1 < RELEASE_ATTEMPTS:
            time.sleep(2**attempt)
    pending = _pending_releases_dir() / f"{release['job_id']}.json"
    pending.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(pending, release, mode=0o600)
    print(
        f"oci: warning: could not release the debug agent lease ({error}); saved it to "
        f"{pending} and the next `oci workflow debug --image` run sends it again",
        file=sys.stderr,
    )


def _send_release(release: dict[str, Any]) -> None:
    complete_debug_agent_lease(
        release["workspace_id"],
        release["workflow_id"],
        release["job_id"],
        release["token"],
        release["status_value"],
        agent_credential=release["agent_credential"],
        expected_credential_version=release["expected_credential_version"],
    )


def _flush_pending_releases() -> None:
    """Send releases an earlier run saved because the cloud was unreachable."""
    directory = _pending_releases_dir()
    if not directory.is_dir():
        return
    for pending in sorted(directory.glob("*.json")):
        try:
            _send_release(json.loads(pending.read_text(encoding="utf-8")))
        except CloudRequestError as exc:
            if exc.transient:
                print(f"oci: warning: {pending} is still unsent: {exc}", file=sys.stderr)
                continue
            print(f"oci: warning: dropped {pending}: {exc}", file=sys.stderr)
        except (ExecutionError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"oci: warning: {pending} is still unsent: {exc}", file=sys.stderr)
            continue
        pending.unlink(missing_ok=True)


@contextmanager
def _agent_lease_heartbeat(
    workspace_id: str,
    workflow_id: str,
    agent_lease: dict[str, Any],
    *,
    interval: float = AGENT_RENEW_INTERVAL_SECONDS,
) -> Iterator[None]:
    """Renew the agent lease while the run holds it.

    A lapsed lease lets a cloud run lease the same connection with the same
    single-use refresh token. Renewal keeps retrying through transient errors
    and stops only once the cloud says the lease is gone.
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                renew_debug_agent_lease(
                    workspace_id,
                    workflow_id,
                    str(agent_lease["job_id"]),
                    agent_lease["token"],
                    AGENT_RENEW_TTL_SECONDS,
                )
            except CloudRequestError as exc:
                if not exc.transient:
                    print(
                        f"oci: warning: {exc}; the agent connection is no longer "
                        "exclusive to this run",
                        file=sys.stderr,
                    )
                    return
                print(f"oci: warning: {exc}; retrying", file=sys.stderr)
            except ExecutionError as exc:
                print(f"oci: warning: {exc}; retrying", file=sys.stderr)

    thread = threading.Thread(target=beat, name="oci-agent-lease-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # Wait out a renewal in flight: it and the release that follows would
        # otherwise race to refresh the same single-use OutcomeCI session token.
        thread.join()


def _run_in_image(
    image: str,
    root: Path,
    config: Path,
    lease: dict[str, Any],
    hold: _AgentHold,
    *,
    name: str,
    payload: dict[str, Any],
    agent: str | None,
    model: str | None,
    auto_continue: bool,
    network: str | None = None,
    retry_run: str | None = None,
) -> dict[str, Any]:
    """Run inside the runner image and bring the run's state back to --dir.

    Every secret crosses into the container on stdin, never argv or env, which
    `docker inspect` and process listings expose.
    """
    bundle = {
        "config": str(config.relative_to(root)),
        "trigger": name,
        "payload": payload,
        "retry": retry_run,
        "agent": agent,
        "model": model,
        "auto_continue": auto_continue,
        "values": lease["values"],
        "expires_at": lease["expires_at"],
        "credentials": [
            {"provider": item["provider"], "credential": item["credential"]}
            for item in hold.agent_leases
        ],
    }
    container = f"oci-debug-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--init",
        "--name",
        container,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        f"HOME={CONTAINER_OUTPUT}/{OUTPUT_HOME}",
        # Arbitrary host uids have no passwd entry in the image.
        "-e",
        "USER=oci",
        "-e",
        "LOGNAME=oci",
        "--mount",
        f"type=bind,src={root},dst={CONTAINER_SOURCE},readonly",
        "--mount",
        f"type=bind,src={hold.output},dst={CONTAINER_OUTPUT}",
        "-w",
        CONTAINER_OUTPUT,
        *(["--network", network] if network else []),
        "--entrypoint",
        "/opt/oci/bin/python",
        image,
        "-m",
        "outcomeci.debug_container",
    ]
    try:
        returncode = _run_container(command, container, json.dumps(bundle))
    finally:
        _import_run_state(hold.output / OUTPUT_WORK, root)
    result_file = hold.output / "result.json"
    if returncode != 0 or not result_file.is_file():
        raise ExecutionError(f"the debug run failed inside {image} (exit {returncode})")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    hold.status = "failed" if result.get("status") == "error" else "completed"
    return result


def _run_container(command: list[str], container: str, stdin: str) -> int:
    """Run the container to completion, stopping it if this process is interrupted.

    Killing only the docker client leaves the container running with the agent
    credential while the lease is released, so an interrupt stops the
    container itself before the lease is let go.
    """
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise ExecutionError("docker is not installed or not on PATH") from exc
    try:
        process.communicate(stdin)
    except BaseException:
        _stop_container(container, process)
        raise
    return process.returncode


def _stop_container(container: str, process: subprocess.Popen[str]) -> None:
    try:
        subprocess.run(
            ["docker", "kill", "--signal", "INT", container], capture_output=True, check=False
        )
        process.wait(timeout=CONTAINER_STOP_GRACE_SECONDS)
    except BaseException:
        pass
    finally:
        subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=False)
        with suppress(BaseException):
            process.wait(timeout=CONTAINER_STOP_GRACE_SECONDS)


def _import_run_state(work: Path, root: Path) -> None:
    """Copy the run's outcome and journal state from the container's copy to --dir.

    Only regular files under a run's own directory come back; the container
    wrote them, so symlinks and unexpected names are skipped.
    """
    for state_dir in RUN_STATE_DIRS:
        source = work / ".outcomeci" / state_dir
        if source.is_symlink() or not source.is_dir():
            continue
        for run_dir in source.iterdir():
            if run_dir.is_symlink() or not run_dir.is_dir() or not _RUN_ID.match(run_dir.name):
                continue
            target = root / ".outcomeci" / state_dir / run_dir.name
            for dirpath, dirnames, filenames in os.walk(run_dir):
                here = Path(dirpath)
                dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
                for filename in filenames:
                    file = here / filename
                    if file.is_symlink() or not file.is_file():
                        continue
                    destination = target / file.relative_to(run_dir)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, destination)


def default_image() -> str:
    """The runner image published with this CLI release."""
    from . import __version__

    if not _RELEASE.match(__version__):
        raise ExecutionError(f"oci {__version__} is not a release build; pass --image")
    return f"{RUNNER_IMAGE}:{__version__}"


def _default_trigger(compiled: dict[str, Any]) -> str:
    triggers = compiled["triggers"]
    if len(triggers) == 1:
        return next(iter(triggers))
    manual = [name for name, item in triggers.items() if item["type"] == "manual"]
    if len(manual) == 1:
        return manual[0]
    raise ExecutionError(f"pass --trigger; the workflow declares {', '.join(sorted(triggers))}")


def _vault_references(compiled: dict[str, Any]) -> list[str]:
    """The vault: references a run's API calls resolve."""
    spec = compiled["workflow"]["spec"]
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.startswith("vault:"):
            found.add(value)

    walk(spec.get("connections") or [])
    walk(spec.get("integrations") or {})
    return sorted(found)


def _local_values(root: Path, compiled: dict[str, Any]) -> dict[str, Any]:
    from . import local_vault

    values = {}
    for reference in _vault_references(compiled):
        path = reference.removeprefix("vault:")
        try:
            values[path] = local_vault.resolve(root, reference)
        except ExecutionError as exc:
            raise ExecutionError(
                f"{exc}; store it with `oci vault local put {path} --value-stdin`"
            ) from exc
    return values


def _codex_auth_path() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured).expanduser() if configured else Path.home() / ".codex") / "auth.json"


def _local_login(root: Path, provider: str) -> dict[str, Any]:
    """This machine's login for one agent provider, as a cloud lease would carry it."""
    if provider == "codex":
        path = _codex_auth_path()
        try:
            credential = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionError(f"no Codex login at {path}; run `codex login` first") from exc
        return {"provider": provider, "credential": credential}
    variable = AGENT_ENV.get(provider)
    if variable is None:
        raise ExecutionError(f"unsupported agent {provider!r}")
    value = os.environ.get(variable)
    if not value:
        from . import local_vault

        try:
            value = local_vault.resolve(root, f"vault:agents/{provider}")
        except ExecutionError as exc:
            raise ExecutionError(
                f"no {provider} login: set {variable}, or store it with "
                f"`oci vault local put agents/{provider} --value-stdin`"
            ) from exc
    return {"provider": provider, "credential": value}


def _write_back_codex(hold: _AgentHold) -> None:
    """Keep a Codex login the run rotated: the old refresh token is spent."""
    for login in hold.agent_leases:
        rotated = _codex_rotation(hold.output / OUTPUT_HOME, login)
        if rotated is not None:
            atomic_write_json(_codex_auth_path(), rotated, mode=0o600)


def run_local(
    root: Path,
    config: Path,
    *,
    trigger_name: str | None = None,
    payload_path: Path | None = None,
    agent: str | None = None,
    model: str | None = None,
    auto_continue: bool = False,
    image: str | None = None,
    network: str | None = None,
    retry_run: str | None = None,
) -> dict[str, Any]:
    """Run the workflow in the runner image with local Vault values and local logins."""
    root, config = root.resolve(), config.resolve()
    if not config.is_relative_to(root):
        raise ExecutionError("the workflow file must live inside --dir")
    compiled = compile_workflow(config)
    image = image or default_image()
    if retry_run is not None:
        _retryable(root, retry_run)
        name, payload = None, None
    else:
        name, payload = _resolve_trigger(
            compiled, trigger_name or _default_trigger(compiled), payload_path
        )
    values = _local_values(root, compiled)
    logins = [_local_login(root, provider) for provider in _runners(compiled, agent)]
    _check_image(image)
    expires_at = (datetime.now(UTC) + timedelta(seconds=LOCAL_RUN_TTL_SECONDS)).isoformat()
    with ExitStack() as held:
        held.enter_context(_termination_interrupts())
        output = Path(held.enter_context(tempfile.TemporaryDirectory(prefix="oci-run-")))
        os.chmod(output, 0o700)
        hold = _AgentHold(logins, output)
        what = f"run {retry_run}" if retry_run else f"trigger {name!r}"
        print(f"Running {what} inside {image} with the local Vault.", file=sys.stderr)
        try:
            return _run_in_image(
                image,
                root,
                config,
                {"values": values, "expires_at": expires_at},
                hold,
                name=name,
                payload=payload,
                agent=agent,
                model=model,
                auto_continue=auto_continue,
                network=network,
                retry_run=retry_run,
            )
        finally:
            _write_back_codex(hold)
