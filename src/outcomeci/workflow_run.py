"""Run a workflow inside the runner container: `oci workflow run`.

Every run executes in the runner image through the same path a cloud run
uses. By default its secrets come from the checkout's local Vault and its
agent login from this machine. With `--cloud`, secrets come from a short-lived
lease on the workspace's Vault (gated server-side on the same permission as
managing that workflow's Vault grants) and the agent login is the workspace's
connected agent, held exclusively for the run as a cloud run holds it.
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
    credentials_path,
    issue_debug_lease,
    renew_debug_agent_lease,
    rotate_debug_vault_credential,
)
from .config import compile_workflow
from .process import ExecutionError
from .run_container import (
    CONTAINER_OUTPUT,
    CONTAINER_SOURCE,
    OUTPUT_HOME,
    OUTPUT_WORK,
    ROTATIONS,
)
from .security import atomic_write_json

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
# The API host each agent must reach from inside the container.
AGENT_HOSTS = {
    "codex": "api.openai.com",
    "claude": "api.anthropic.com",
    "opencode": "openrouter.ai",
}
NETWORK_PROBE_TIMEOUT_SECONDS = 60
_RESOLVE_HOSTS = """
import socket, sys
failed = []
for host in sys.argv[1:]:
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        failed.append(host)
print("\\n".join(failed))
sys.exit(1 if failed else 0)
"""
_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
            "schedule_arn": "arn:oci:local:schedule",
            "scheduled_at": datetime.now(UTC).isoformat(),
            "execution_id": f"local-{uuid.uuid4()}",
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


def _check_agent(agent: str | None, model: str | None) -> None:
    """OpenCode runs a model through OpenRouter, so it needs one named explicitly."""
    if agent == "opencode" and not (model or "").startswith("openrouter/"):
        raise ExecutionError("--agent opencode needs --model openrouter/<provider>/<model>")


def _runners(compiled: dict[str, Any], agent: str | None) -> list[str]:
    """Every agent provider the run needs a login for: the default first, then any
    runner a step selects for itself, or only `agent` when it overrides them all."""
    if agent:
        return [agent]
    found = [_default_agent(compiled)]
    for step in (compiled.get("instructions") or {}).get("steps", {}).values():
        runner = (step.get("policy") or {}).get("runner")
        if runner and runner not in found:
            found.append(runner)
    return found


def _resolve_trigger(
    compiled: dict[str, Any], trigger_name: str | None, payload_path: Path | None
) -> tuple[str, dict[str, Any]]:
    definition = compiled["triggers"].get(trigger_name)
    if definition is None:
        raise ExecutionError(f"workflow does not declare trigger {trigger_name}")
    payload = (
        json.loads(payload_path.read_text(encoding="utf-8"))
        if payload_path is not None
        else _synthesize_payload(trigger_name, definition)
    )
    return trigger_name, payload


def run_cloud(
    root: Path,
    config: Path,
    workspace_id: str,
    workflow_id: str,
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
    """Run in the runner image with the workspace's Vault and connected agent."""
    root, config = root.resolve(), config.resolve()
    if not config.is_relative_to(root):
        raise ExecutionError("the workflow file must live inside --dir")
    compiled = compile_workflow(config)
    _check_agent(agent, model)
    # Everything that can fail on the user's input fails here, before a lease
    # takes the workspace's agent connection away from its cloud runs.
    image = image or default_image()
    _check_image(image)
    _check_network(image, network, _agent_hosts(_runners(compiled, agent)))
    _flush_pending_releases()
    _flush_pending_rotations()
    if retry_run is not None:
        _retryable(root, retry_run)
        name, payload = None, None
    else:
        name, payload = _resolve_trigger(
            compiled, trigger_name or _default_trigger(compiled), payload_path
        )
    leases = _issue_leases(workspace_id, workflow_id, _runners(compiled, agent))
    lease = leases[0]

    with ExitStack() as held:
        held.enter_context(_termination_interrupts())
        hold = held.enter_context(_hold_agent_lease(workspace_id, workflow_id, leases))
        what = f"run {retry_run}" if retry_run else f"trigger {name!r}"
        print(
            f"Running {what} inside {image} with workspace {workspace_id}'s Vault "
            f"for workflow {workflow_id}.",
            file=sys.stderr,
        )
        try:
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
        finally:
            _save_cloud_rotations(workspace_id, workflow_id, lease, hold.output)
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


def _issue_leases(workspace_id: str, workflow_id: str, runners: list[str]) -> list[dict[str, Any]]:
    """One Vault lease per runner the run needs, each holding that runner's login.

    When a later one is refused, the logins already leased are released before
    the refusal is raised.
    """
    leases: list[dict[str, Any]] = []
    try:
        for runner in runners:
            leases.append(
                issue_debug_lease(
                    workspace_id,
                    workflow_id,
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
    """Fail before any lease when the image cannot run a workflow at all."""
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
                "import outcomeci.run_container",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ExecutionError("docker is not installed or not on PATH") from exc
    if probe.returncode != 0:
        detail = (probe.stderr.strip().splitlines() or ["no output"])[-1]
        raise ExecutionError(
            f"{image} cannot run workflows; it needs an OutcomeCI runner build "
            f"that includes outcomeci.run_container ({detail})"
        )


def _agent_hosts(runners: list[str]) -> list[str]:
    return [AGENT_HOSTS[runner] for runner in runners if runner in AGENT_HOSTS]


def _check_network(image: str, network: str | None, hosts: list[str]) -> None:
    """Fail before the run when the container cannot resolve the hosts its agents need.

    An agent that cannot reach its API retries quietly for a long time, so a
    Docker network without working DNS makes a run look like a silent hang.
    Resolving the hosts in a throwaway container catches that in a second.
    """
    if not hosts:
        return
    command = [
        "docker",
        "run",
        "--rm",
        *(["--network", network] if network else []),
        "--entrypoint",
        "/opt/oci/bin/python",
        image,
        "-c",
        _RESOLVE_HOSTS,
        *hosts,
    ]
    try:
        probe = subprocess.run(
            command, capture_output=True, text=True, timeout=NETWORK_PROBE_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise ExecutionError("docker is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired:
        unresolved = hosts
    else:
        if probe.returncode == 0:
            return
        unresolved = [line for line in probe.stdout.splitlines() if line.strip()] or hosts
    where = f"the {network!r} network" if network else "Docker's default bridge network"
    advice = (
        "pass --network host so the container uses this machine's DNS"
        if network != "host"
        else "check this machine's DNS settings"
    )
    raise ExecutionError(
        f"the container cannot resolve {', '.join(unresolved)} on {where}, "
        f"so the agent could not reach its API; {advice}"
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
        raise ExecutionError("the Vault lease carried no agent login for the run")
    with tempfile.TemporaryDirectory(prefix="oci-run-") as output:
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


def _rotations(output: Path) -> dict[str, dict[str, str]]:
    """The Vault secrets the run rotated, by path, as the container recorded them."""
    try:
        value = json.loads((output / ROTATIONS).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_local_rotations(root: Path, output: Path) -> None:
    """Write rotated secrets back to the local Vault the run's values came from."""
    from . import local_vault

    for path, secrets in _rotations(output).items():
        try:
            local_vault.rotate(root, f"vault:{path}", secrets)
        except ExecutionError as exc:
            print(
                f"oci: warning: could not save the rotated secret for {path} to the local "
                f"Vault ({exc}); store it again with `oci vault local put {path}`",
                file=sys.stderr,
            )


def _pending_rotations_dir() -> Path:
    return credentials_path().parent / "vault-rotations"


def _save_cloud_rotations(
    workspace_id: str, workflow_id: str, lease: dict[str, Any], output: Path
) -> None:
    """Save rotated secrets to the workspace Vault. A rotation that cannot land
    now is kept on disk and sent again before the next cloud run: the provider
    has already revoked the secret it replaced."""
    versions = dict(lease.get("versions") or {})
    for path, secrets in _rotations(output).items():
        rotation = {
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "lease_id": str(lease.get("lease_id", "")),
            "path": path,
            "expected_version": int(versions.get(path, 0)),
            "secrets": secrets,
        }
        try:
            _send_rotation(rotation)
        except (CloudRequestError, ExecutionError, KeyError, ValueError) as exc:
            pending = _pending_rotations_dir() / f"{uuid.uuid4().hex}.json"
            pending.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write_json(pending, rotation, mode=0o600)
            print(
                f"oci: warning: could not save the rotated secret for {path} ({exc}); "
                f"kept it in {pending} and the next `oci workflow run --cloud` sends it again",
                file=sys.stderr,
            )


def _send_rotation(rotation: dict[str, Any]) -> None:
    rotate_debug_vault_credential(
        rotation["workspace_id"],
        rotation["workflow_id"],
        rotation["lease_id"],
        rotation["path"],
        rotation["expected_version"],
        rotation["secrets"],
    )


def _flush_pending_rotations() -> None:
    directory = _pending_rotations_dir()
    if not directory.is_dir():
        return
    for pending in sorted(directory.glob("*.json")):
        try:
            _send_rotation(json.loads(pending.read_text(encoding="utf-8")))
        except CloudRequestError as exc:
            if exc.transient:
                print(f"oci: warning: {pending} is still unsent: {exc}", file=sys.stderr)
                continue
            print(f"oci: warning: dropped {pending}: {exc}", file=sys.stderr)
        except (ExecutionError, OSError, KeyError, TypeError, ValueError) as exc:
            print(f"oci: warning: {pending} is still unsent: {exc}", file=sys.stderr)
            continue
        pending.unlink(missing_ok=True)


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
        f"oci: warning: could not release the agent login lease ({error}); saved it to "
        f"{pending} and the next `oci workflow run --cloud` sends it again",
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
    container = f"oci-run-{uuid.uuid4().hex[:12]}"
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
        "outcomeci.run_container",
    ]
    try:
        returncode = _run_container(command, container, json.dumps(bundle))
    finally:
        _import_run_state(hold.output / OUTPUT_WORK, root)
    result_file = hold.output / "result.json"
    if returncode != 0 or not result_file.is_file():
        raise ExecutionError(f"the run failed inside {image} (exit {returncode})")
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
    _check_agent(agent, model)
    image = image or default_image()
    if retry_run is not None:
        _retryable(root, retry_run)
        name, payload = None, None
    else:
        name, payload = _resolve_trigger(
            compiled, trigger_name or _default_trigger(compiled), payload_path
        )
    values = _local_values(root, compiled)
    runners = _runners(compiled, agent)
    logins = [_local_login(root, provider) for provider in runners]
    _check_image(image)
    _check_network(image, network, _agent_hosts(runners))
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
            _save_local_rotations(root, hold.output)
