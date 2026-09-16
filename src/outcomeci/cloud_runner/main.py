"""Entrypoint for private authorization and execution tasks."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from ..process import ExecutionError
from .client import CoreClient, CoreError
from .models import ContractError, Launch
from .process import run
from .providers import ADAPTERS

URL = re.compile(r"https://[^\s<>'\"\x00-\x1f\x7f]+")
USER_CODE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
CLAUDE_TOKEN = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HEARTBEAT_INTERVAL_SECONDS = 15.0


def workflow_failure_category(error: Exception) -> str:
    """Return an operator-safe category without emitting workflow or provider output."""
    if isinstance(error, ExecutionError):
        message = str(error).casefold()
        if "authentication" in message or "unauthorized" in message or "oauth" in message:
            return "provider_auth_rejected"
        if "bubblewrap" in message or "sandbox" in message:
            return "runner_sandbox_unavailable"
        if "failed with exit" in message:
            return "agent_process_failed"
        if "output" in message or "artifact" in message:
            return "workflow_artifact_invalid"
        return "workflow_execution_failed"
    if isinstance(error, ContractError):
        return "workflow_contract_failed"
    return "internal_failure"


def execute_workflow(launch: Launch, client: CoreClient) -> int:
    """Execute one immutable generic workflow claim with broker-private credentials."""
    from .. import local
    from ..config import compile_workflow

    claim = client.claim_workflow()
    lease = str(claim["lease_token"])
    root = Path(
        tempfile.mkdtemp(
            prefix="oci-cloud-workflow-",
            dir=os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner"),
        )
    )
    os.chmod(root, 0o700)
    previous: dict[str, str | None] = {}
    stop = threading.Event()
    run_id: str | None = None
    heartbeat_lock = threading.Lock()
    heartbeat_failure: list[Exception] = []
    agent_update = None
    credential_version = None
    try:
        config = root / "outcome.yml"
        config.write_text(str(claim["content"]), encoding="utf-8")
        for name, encoded in dict(claim.get("files") or {}).items():
            target = (root / str(name)).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ContractError("workflow support file escaped its root")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded, validate=True))

        agent = dict(claim["agent"])
        provider = str(agent["provider"])
        credential = agent["credential"]
        credential_version = int(agent["credential_version"])
        if provider == "codex":
            home = root / ".codex"
            home.mkdir(mode=0o700)
            (home / "auth.json").write_text(
                json.dumps(credential, separators=(",", ":")), encoding="utf-8"
            )
            os.chmod(home / "auth.json", 0o600)
            (home / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\n', encoding="utf-8"
            )
            injected = {"CODEX_HOME": str(home)}
        elif provider == "claude":
            injected = {"CLAUDE_CODE_OAUTH_TOKEN": str(credential)}
        elif provider == "opencode":
            injected = {"OPENROUTER_API_KEY": str(credential)}
        else:
            raise ContractError("unsupported workflow agent")
        for key, value in injected.items():
            previous[key] = os.environ.get(key)
            os.environ[key] = value

        values = dict((claim.get("vault") or {}).get("values") or {})
        vault_expires = datetime.fromisoformat(str((claim.get("vault") or {})["expires_at"]))

        def resolver(reference: str):
            if datetime.now(UTC) >= vault_expires:
                raise ContractError("workflow credential lease expired")
            if not reference.startswith("vault:"):
                raise ContractError("cloud credentials must use vault references")
            path = reference.removeprefix("vault:")
            if path not in values:
                raise ContractError("credential is not granted to this workflow")
            return values[path]

        client.workflow_start(lease)

        def policy_event(event: dict) -> None:
            with heartbeat_lock:
                response = client.workflow_heartbeat(lease, [event])
            if response.get("policy_events_received") != 1:
                raise CoreError("policy_evidence_unacknowledged", True)

        def created(value: str) -> None:
            nonlocal run_id
            run_id = value

        def pulse() -> None:
            while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                try:
                    with heartbeat_lock:
                        client.workflow_heartbeat(lease)
                except CoreError as exc:
                    heartbeat_failure.append(exc)
                    return

        heartbeat = threading.Thread(target=pulse, daemon=True)
        heartbeat.start()
        result = local.trigger(
            root,
            config,
            str(claim["trigger_name"]),
            dict(claim["input"]),
            on_created=created,
            credential_resolver=resolver,
            event_sink=policy_event,
            execution_backend="outcomeci",
            _container_isolated=True,
        )
        run_id = str(result["run_id"])
        if heartbeat_failure:
            raise CoreError("policy_evidence_upload_failed", True)
        if result.get("status") == "error":
            raise ContractError("workflow recorded an error")
        phase_count = len(compile_workflow(config)["instructions"]["phases"])
        if len(result.get("completed_phases", [])) != phase_count:
            raise ContractError("workflow requires a durable continuation")
        if provider == "codex":
            agent_update = json.loads((root / ".codex" / "auth.json").read_text())
        client.workflow_complete(
            lease,
            "completed",
            run_id=run_id,
            expected_credential_version=credential_version,
            agent_credential=agent_update,
        )
        return 0
    except Exception as exc:
        auth_path = root / ".codex" / "auth.json"
        if credential_version is not None and auth_path.is_file():
            with suppress(OSError, json.JSONDecodeError):
                agent_update = json.loads(auth_path.read_text())
        with suppress(CoreError):
            client.workflow_complete(
                lease,
                "failed",
                run_id=run_id,
                category=workflow_failure_category(exc),
                expected_credential_version=credential_version,
                agent_credential=agent_update,
            )
        raise
    finally:
        stop.set()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(root, ignore_errors=True)


def reconcile_failure(client: CoreClient, claim: object, category: str, retryable: bool) -> None:
    """Best-effort terminal reconciliation after an execution has been claimed."""
    with suppress(CoreError):
        client.fail(
            claim.completion_token,
            category,
            retryable,
            claim.lease_id,
        )


def safe_verification(provider: str, output: str) -> tuple[str, str | None] | None:
    output = ANSI_ESCAPE.sub("", output)
    url = URL.search(output)
    code = USER_CODE.search(output)
    if not url or (provider == "codex" and not code):
        return None
    return url.group(0).rstrip(".,)"), code.group(0) if code else None


def safe_env(
    root: Path, github_token: str | None = None, core_job_token: str | None = None
) -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update({"HOME": str(root), "TMPDIR": str(root / "tmp")})
    if github_token:
        env["GH_TOKEN"] = github_token
        env["GITHUB_TOKEN"] = github_token
    if core_job_token:
        env["OUTCOMECI_API_KEY"] = core_job_token
    return env


def classify_failure(
    output: str, *, authorization: bool, cancelled: bool = False
) -> tuple[str, bool]:
    if cancelled:
        return ("authorization_cancelled" if authorization else "agent_cancelled"), True
    lowered = output.lower()
    if authorization:
        if "denied" in lowered or "declined" in lowered:
            return "provider_denied", False
        if "expired" in lowered or "timed out" in lowered:
            return "authorization_expired", False
        if "disabled" in lowered or "administrator" in lowered or "not available" in lowered:
            return "provider_unavailable", False
        return "authorization_failed", True
    if any(
        term in lowered
        for term in (
            "authentication",
            "unauthorized",
            "oauth",
            "token expired",
            "login required",
            "failed to refresh token",
            "refresh token was already used",
        )
    ):
        return "provider_auth_rejected", False
    return "agent_process_failed", False


def authorize(launch: Launch, client: CoreClient) -> int:
    claim = client.claim_authorization()
    root = Path(
        tempfile.mkdtemp(
            prefix="oci-agent-auth-",
            dir=os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner"),
        )
    )
    os.chmod(root, 0o700)
    try:
        (root / "tmp").mkdir(mode=0o700)
        env = safe_env(root)
        if claim.provider == "codex":
            codex_home = root / "codex"
            codex_home.mkdir(mode=0o700)
            (codex_home / "config.toml").write_text(
                'cli_auth_credentials_store = "file"\n', encoding="utf-8"
            )
            os.chmod(codex_home / "config.toml", 0o600)
            env["CODEX_HOME"] = str(codex_home)
        published = False
        pending = ""

        def progress(chunk: str) -> None:
            nonlocal published, pending
            if published:
                return
            pending = (pending + chunk)[-32_768:]
            verification = safe_verification(claim.provider, pending)
            if verification:
                client.verification(
                    claim.session_token,
                    verification[0],
                    verification[1],
                    claim.expires_at,
                )
                published = True

        next_response_poll = 0.0

        def response_input() -> str | None:
            nonlocal next_response_poll
            if claim.provider != "claude" or not published or time.monotonic() < next_response_poll:
                return None
            next_response_poll = time.monotonic() + 2.0
            return client.authorization_response(claim.session_token)

        result = run(
            claim.command,
            cwd=root,
            env=env,
            timeout=900,
            on_output=progress,
            terminal=True,
            input_provider=response_input,
        )
        if result.returncode:
            category, retryable = classify_failure(
                result.stdout + result.stderr,
                authorization=True,
                cancelled=result.returncode < 0,
            )
            client.fail(claim.session_token, category, retryable)
            return result.returncode
        if claim.provider == "codex":
            auth_path = root / "codex" / "auth.json"
            credential = json.loads(auth_path.read_text(encoding="utf-8"))
            if not isinstance(credential, dict):
                raise ContractError("invalid Codex credential")
        else:
            transcript = CONTROL_CHAR.sub("", ANSI_ESCAPE.sub("", result.stdout + result.stderr))
            found = CLAUDE_TOKEN.search(transcript)
            if not found:
                client.fail(claim.session_token, "invalid_result", False)
                raise ContractError("invalid Claude credential")
            credential = found.group(0)
        client.complete(claim.session_token, {"provider": claim.provider, "credential": credential})
        return 0
    except TimeoutError:
        client.fail(claim.session_token, "authorization_expired", False)
        raise
    finally:
        shutil.rmtree(root, ignore_errors=True)


def execute(launch: Launch, client: CoreClient) -> int:
    claim = client.claim_execution()
    adapter = ADAPTERS[claim.provider]
    workflow_phase = str(claim.outcome.get("phase") or "unknown")
    log_sequence = 0

    def lifecycle(
        event_type: str,
        message: str,
        *,
        level: str = "info",
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        nonlocal log_sequence
        log_sequence += 1
        with suppress(CoreError):
            client.log(
                claim.completion_token,
                {
                    "sequence": log_sequence,
                    "phase": workflow_phase,
                    "level": level,
                    "event_type": event_type,
                    "message": message,
                    "metadata": metadata or {},
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )

    lifecycle("runner.claimed", "Runner claimed the workflow phase.")
    root = Path(
        tempfile.mkdtemp(
            prefix="oci-outcome-",
            dir=os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner"),
        )
    )
    os.chmod(root, 0o700)
    try:
        (root / "tmp").mkdir(mode=0o700)
        env = adapter.hydrate(claim, root, safe_env(root, claim.github_token, claim.core_job_token))
        env.update(
            {
                "OUTCOMECI_API_URL": launch.core_url,
                "OUTCOMECI_JOB_ID": claim.job["job_id"],
                "OUTCOMECI_JOB_TOKEN": claim.core_job_token,
            }
        )
        workspace = Path(os.environ.get("AGENT_WORK_ROOT", "/workspace"))
        if not workspace.is_dir():
            raise ContractError("runner workspace is unavailable")
        (workspace / "outcome-claim.json").write_text(
            json.dumps(claim.outcome, separators=(",", ":")), encoding="utf-8"
        )
        phase = "preparing"
        detail: str | None = "Preparing the Outcome workspace"
        last_heartbeat = 0.0

        def heartbeat() -> None:
            nonlocal last_heartbeat
            now = time.monotonic()
            if now - last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
                return
            try:
                client.heartbeat(claim.completion_token, claim.lease_id, phase, detail)
            except CoreError as error:
                if not error.retryable:
                    raise
                return
            last_heartbeat = now

        def tick() -> None:
            heartbeat()

        heartbeat()
        lifecycle(
            "agent.started",
            "Coding agent started.",
            metadata={"provider": claim.provider},
        )
        result = run(
            claim.command,
            cwd=workspace,
            env=env,
            timeout=claim.timeout_seconds,
            on_tick=tick,
        )
        try:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            outcome_result = json.loads(lines[-1]) if lines else None
            if not isinstance(outcome_result, dict) or outcome_result.get("status") not in {
                "awaiting_confirmation",
                "ready_for_implementation",
                "completed",
            }:
                raise ContractError("invalid outcome result")
            lifecycle(
                "agent.completed",
                "Coding agent completed the workflow phase.",
                metadata={"status": str(outcome_result["status"])},
            )
            client.complete(claim.completion_token, {"result": outcome_result})
            return 0
        except (ContractError, json.JSONDecodeError):
            category, retryable = classify_failure(
                result.stdout + result.stderr,
                authorization=False,
                cancelled=result.returncode < 0,
            )
            lifecycle(
                "agent.failed",
                "Coding agent did not complete the workflow phase.",
                level="error",
                metadata={"category": category, "retryable": retryable},
            )
            client.fail(claim.completion_token, category, retryable, claim.lease_id)
            return result.returncode or 1
    except TimeoutError:
        lifecycle("runner.timed_out", "Workflow phase timed out.", level="error")
        reconcile_failure(client, claim, "agent_timeout", True)
        raise
    except CoreError as error:
        lifecycle(
            "runner.failed",
            "Runner could not communicate with the control plane.",
            level="error",
            metadata={"category": error.category, "retryable": error.retryable},
        )
        reconcile_failure(client, claim, error.category, error.retryable)
        raise
    except (ContractError, KeyError, json.JSONDecodeError):
        lifecycle("runner.invalid_job", "Runner rejected the workflow job.", level="error")
        reconcile_failure(client, claim, "invalid_job", False)
        raise
    except Exception:
        lifecycle("runner.failed", "Runner encountered an internal failure.", level="error")
        reconcile_failure(client, claim, "internal_failure", True)
        raise
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "oci":
        os.execvp("oci", args)
    exception_type: str | None = None
    try:
        launch = Launch.from_env(dict(os.environ))
        client = CoreClient(launch.core_url, launch.job_id, launch.bootstrap_token, launch.mode)
        if launch.mode == "authorize":
            return authorize(launch, client)
        if launch.mode == "workflow":
            return execute_workflow(launch, client)
        return execute(launch, client)
    except TimeoutError:
        category, retryable = "agent_timeout", True
    except CoreError as error:
        category, retryable = error.category, error.retryable
    except (ContractError, KeyError, json.JSONDecodeError):
        category, retryable = "invalid_job", False
    except ExecutionError as error:
        category, retryable = workflow_failure_category(error), error.retryable
    except Exception as error:
        category, retryable = "internal_failure", True
        exception_type = type(error).__name__
    payload = {
        "event": "outcome_runner_failed",
        "category": category,
        "retryable": retryable,
    }
    if category == "internal_failure":
        payload["exception_type"] = exception_type
    print(json.dumps(payload), file=sys.stderr)
    return 1
