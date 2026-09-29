"""Entrypoint for private authorization and execution tasks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyte

from ..leases import LeaseResolver
from ..process import ExecutionError
from ..publication import REPORT, REQUIREMENTS, prepare_publication
from ..security import private_path
from .client import CoreClient, CoreError
from .models import ContractError, Launch
from .process import PTY_COLUMNS, PTY_ROWS, run
from .providers import ADAPTERS
from .providers.codex import FILE_AUTH_CONFIG as CODEX_FILE_AUTH_CONFIG
from .redaction import redact_diagnostic

URL = re.compile(r"https://[^\s<>'\"\x00-\x1f\x7f]+")
USER_CODE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CLAUDE_TOKEN = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")
HEARTBEAT_INTERVAL_SECONDS = 15.0
WORKFLOW_ARTIFACT_FILE_LIMIT = 200
WORKFLOW_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
WORKFLOW_ARTIFACT_TOTAL_BYTES = 20 * 1024 * 1024
# Waits between attempts to report a final workflow result: 6 attempts over
# about a minute, inside the lease the heartbeat keeps alive.
COMPLETION_REPORT_DELAYS_SECONDS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)


def workflow_artifacts(root: Path, run_id: str) -> list[dict[str, str]]:
    """Return the bounded, credential-free durable bundle for one workflow run."""
    outcome_root = (root / ".outcomeci" / "outcomes" / run_id).resolve()
    expected_root = (root / ".outcomeci" / "outcomes").resolve()
    if not outcome_root.is_relative_to(expected_root) or not outcome_root.is_dir():
        raise ContractError("workflow run artifact directory is unavailable")
    artifacts: list[dict[str, str]] = []
    total = 0
    for path in sorted(item for item in outcome_root.rglob("*") if item.is_file()):
        relative_to_outcome = path.relative_to(outcome_root)
        if private_path(relative_to_outcome):
            continue
        content = path.read_bytes()
        total += len(content)
        if (
            len(artifacts) >= WORKFLOW_ARTIFACT_FILE_LIMIT
            or len(content) > WORKFLOW_ARTIFACT_FILE_BYTES
            or total > WORKFLOW_ARTIFACT_TOTAL_BYTES
        ):
            raise ContractError("workflow artifact bundle exceeds the completion limit")
        artifacts.append(
            {
                "path": str(path.relative_to(root)),
                "content_base64": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return artifacts


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


USAGE_LIMIT_PATTERNS = (
    "usage limit",
    "rate limit",
    "rate-limited",
    "quota",
    "too many requests",
    "429",
)


def _is_usage_limit_error(error: Exception) -> bool:
    """Best-effort: agent CLIs don't expose a stable exit code or error type for
    this, only free text in the message a failed process exits with."""
    if not isinstance(error, ExecutionError):
        return False
    message = str(error).casefold()
    return any(pattern in message for pattern in USAGE_LIMIT_PATTERNS)


def _write_private_file(path: Path, content: str) -> None:
    """Write `content` to `path` with 0600 permissions from creation -- no
    window at default permissions the way write_text() + a later chmod()
    has. Overwrites in place (not O_EXCL): unlike CodexAdapter.hydrate,
    which always gets a fresh root, this can run twice in one root when a
    workflow falls back to a different agent after this one already wrote
    its credential here."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def _inject_agent_credential(root: Path, provider: str, credential: Any) -> dict[str, str]:
    if provider == "codex":
        home = root / ".codex"
        home.mkdir(mode=0o700, exist_ok=True)
        _write_private_file(home / "auth.json", json.dumps(credential, separators=(",", ":")))
        _write_private_file(home / "config.toml", CODEX_FILE_AUTH_CONFIG)
        return {"CODEX_HOME": str(home)}
    if provider == "claude":
        return {"CLAUDE_CODE_OAUTH_TOKEN": str(credential)}
    if provider == "opencode":
        return {"OPENROUTER_API_KEY": str(credential)}
    raise ContractError("unsupported workflow agent")


def _report_final(report: Callable[[], None]) -> None:
    """Report a final workflow result, retrying transient Core errors.

    Core's complete handler is idempotent for a repeated status, so a retry
    after a lost response is safe. A non-retryable error, or a retryable one
    that outlasts every attempt, is raised to the caller.
    """
    for delay in (*COMPLETION_REPORT_DELAYS_SECONDS, None):
        try:
            report()
            return
        except CoreError as error:
            if not error.retryable or delay is None:
                raise
        time.sleep(delay)


def _claim_or_skip(claim_fn):
    """Call a claim_*() method, treating a retryable conflict as a no-op.

    Lost the race for this invocation (or the agent connection/lease it
    needs is busy with another one) before ever claiming a lease -- routine
    contention under concurrent/bursty trigger delivery, not a failure of
    this runner. No lease was ever issued, so there is nothing to report
    through the retryable-failure path used once a lease exists (see #58);
    the work is either already progressing under whoever won the claim, or
    still queued for the next attempt. Returns None on this no-op path;
    the caller should exit 0 rather than fall through to a reported
    outcome_runner_failed.
    """
    try:
        return claim_fn()
    except CoreError as error:
        if not error.retryable:
            raise
        print(
            json.dumps({"event": "outcome_runner_claim_skipped", "category": error.category}),
            file=sys.stderr,
        )
        return None


def _completion_report_failed(error: CoreError, status: str) -> None:
    print(
        json.dumps(
            {
                "event": "workflow_completion_report_failed",
                "report_category": error.category,
                "status": status,
                "requeued": False,
            }
        ),
        file=sys.stderr,
    )


def execute_workflow(launch: Launch, client: CoreClient) -> int:
    """Execute one immutable generic workflow claim with broker-private credentials."""
    from .. import local
    from ..config import compile_workflow

    claim = _claim_or_skip(client.claim_workflow)
    if claim is None:
        return 0
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
    provider: str | None = None
    steps_finished = False
    try:
        config = root / "outcome.yml"
        config.write_text(str(claim["content"]), encoding="utf-8")
        for name, encoded in dict(claim.get("files") or {}).items():
            target = (root / str(name)).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ContractError("workflow support file escaped its root")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded, validate=True))

        # One login per runner the workflow's steps use; the default first.
        logins = [dict(item) for item in (claim.get("agents") or [claim["agent"]])]
        for login in logins:
            injected = _inject_agent_credential(root, str(login["provider"]), login["credential"])
            for key, value in injected.items():
                previous.setdefault(key, os.environ.get(key))
                os.environ[key] = value
        # Only a Codex login rotates, so writeback follows whichever login is Codex.
        rotating = next((login for login in logins if login["provider"] == "codex"), logins[0])
        provider = str(rotating["provider"])
        credential_version = int(rotating["credential_version"])
        fallback_spec = compile_workflow(config)["workflow"]["spec"]["agents"]["default"].get(
            "fallback"
        )
        fallback_used = False
        active_agent: str | None = None
        active_model: str | None = None

        vault = dict(claim.get("vault") or {})
        versions = {str(key): int(item) for key, item in dict(vault.get("versions") or {}).items()}

        def save_rotation(path: str, secrets: dict[str, str]) -> None:
            # The provider revoked the secret this replaces: save it before use.
            versions[path] = client.workflow_vault_rotate(
                lease, str(vault["lease_id"]), path, versions.get(path, 0), secrets
            )

        resolver = LeaseResolver(
            dict(vault.get("values") or {}),
            datetime.fromisoformat(str(vault["expires_at"])),
            on_rotate=save_rotation if vault.get("lease_id") else None,
            error=ContractError,
            expired="workflow credential lease expired",
            not_granted="credential is not granted to this workflow",
        )

        client.workflow_start(lease)

        def policy_event(event: dict) -> None:
            with heartbeat_lock:
                response = client.workflow_heartbeat(lease, [event])
            if response.get("policy_events_received") != 1:
                raise CoreError("policy_evidence_unacknowledged", True)

        def policy_review(proposal: dict) -> dict:
            with heartbeat_lock:
                return client.workflow_policy_review(lease, proposal)

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

        def execution_options(
            agent_override: str | None, model_override: str | None
        ) -> local.ExecutionOptions:
            return local.ExecutionOptions(
                agent=agent_override,
                model=model_override,
                credential_resolver=resolver,
                event_sink=policy_event,
                policy_reviewer=policy_review,
                _container_isolated=True,
            )

        def call_trigger(agent_override: str | None, model_override: str | None):
            return local.trigger(
                root,
                config,
                str(claim["trigger_name"]),
                dict(claim["input"]),
                on_created=created,
                options=execution_options(agent_override, model_override),
            )

        def call_continue(agent_override: str | None, model_override: str | None):
            return local.continue_run(
                root,
                config,
                run_id,
                approve=True,
                options=execution_options(agent_override, model_override),
            )

        def execute_call(factory):
            nonlocal provider, credential_version, active_agent, active_model, fallback_used
            try:
                return factory(active_agent, active_model)
            except ExecutionError as exc:
                if fallback_used or fallback_spec is None or not _is_usage_limit_error(exc):
                    raise
                fallback_used = True
                fallback_agent = client.workflow_agent_fallback(lease)
                fallback_injected = _inject_agent_credential(
                    root, str(fallback_agent["provider"]), fallback_agent["credential"]
                )
                for key, value in fallback_injected.items():
                    previous.setdefault(key, os.environ.get(key))
                    os.environ[key] = value
                provider = str(fallback_agent["provider"])
                credential_version = int(fallback_agent["credential_version"])
                active_agent = provider
                active_model = fallback_spec.get("model")
                return local.retry(
                    root,
                    config,
                    run_id,
                    options=execution_options(active_agent, active_model),
                )

        result = execute_call(call_trigger)
        run_id = str(result["run_id"])
        step_count = len(compile_workflow(config)["instructions"]["steps"])
        while len(result.get("completed_steps", [])) != step_count:
            if heartbeat_failure:
                raise CoreError("policy_evidence_upload_failed", True)
            if result.get("status") == "error":
                raise ContractError("workflow recorded an error")
            if not result.get("ready_steps"):
                raise ContractError("workflow requires a durable continuation")
            result = execute_call(call_continue)
        # Every step has run, side effects included. From here on, nothing may
        # requeue the invocation: a retry would run every step again.
        steps_finished = True
        if heartbeat_failure:
            raise CoreError("policy_evidence_upload_failed", True)
        if provider == "codex":
            agent_update = json.loads((root / ".codex" / "auth.json").read_text())
        artifacts = workflow_artifacts(root, run_id)
        try:
            _report_final(
                lambda: client.workflow_complete(
                    lease,
                    "completed",
                    run_id=run_id,
                    artifacts=artifacts,
                    expected_credential_version=credential_version,
                    agent_credential=agent_update,
                )
            )
        except CoreError as completion_error:
            if not completion_error.retryable:
                raise
            # Core stayed unreachable. Report nothing further: a failure report
            # would requeue a finished run or mark a successful one failed.
            _completion_report_failed(completion_error, "completed")
            return 1
        return 0
    except Exception as exc:
        auth_path = root / ".codex" / "auth.json"
        if provider == "codex" and credential_version is not None and auth_path.is_file():
            with suppress(OSError, json.JSONDecodeError):
                agent_update = json.loads(auth_path.read_text())

        category = workflow_failure_category(exc)
        detail = redact_diagnostic(exc)
        retryable = isinstance(exc, CoreError) and exc.retryable and not steps_finished

        def report_failure() -> None:
            client.workflow_complete(
                lease,
                "failed",
                run_id=run_id,
                category=category,
                detail=detail,
                expected_credential_version=credential_version,
                agent_credential=agent_update,
                retryable=retryable,
            )

        try:
            if steps_finished:
                _report_final(report_failure)
            else:
                report_failure()
        except CoreError as completion_error:
            # The original failure (exc) is what gets re-raised below; if the report
            # of it also gets rejected, that's otherwise invisible -- the invocation
            # just dangles until the lease reaper times it out with a generic message,
            # burying the real cause. Surface it here instead.
            print(
                json.dumps(
                    {
                        "event": "workflow_completion_report_failed",
                        "report_category": completion_error.category,
                        "original_category": category,
                    }
                ),
                file=sys.stderr,
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


def render_terminal_screen(raw: str) -> str:
    """Replay captured pty bytes through a virtual terminal and return the
    text as it actually appears on screen.

    Interactive CLIs built on TUI frameworks (Ink and similar) commonly
    redraw by moving the cursor and rewriting only part of a line, rather
    than printing linearly. Stripping escape codes and concatenating what's
    left silently corrupts that output: a cursor move can jump over
    characters a prior frame already drew, and naive concatenation both
    skips those characters and never accounts for later frames overwriting
    earlier ones. A real terminal emulator resolves this correctly because
    it maintains persistent screen state across the whole stream, exactly
    like a human's terminal would.
    """
    screen = pyte.Screen(PTY_COLUMNS, PTY_ROWS)
    pyte.Stream(screen).feed(raw)
    return "\n".join(screen.display)


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
    claim = _claim_or_skip(client.claim_authorization)
    if claim is None:
        return 0
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
            _write_private_file(codex_home / "config.toml", CODEX_FILE_AUTH_CONFIG)
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
            # `claude setup-token` only ever prints the token once ("you won't
            # be able to see it again") -- it does not persist a credentials
            # file the way Codex's auth.json does, so this has to come from
            # the captured transcript. It renders through a TUI that redraws
            # via cursor movement rather than printing linearly, so the raw
            # bytes are replayed through a virtual terminal first: naively
            # stripping escape codes and concatenating what's left can jump
            # over characters a prior frame already drew (confirmed against
            # real output), silently truncating the token.
            transcript = render_terminal_screen(result.stdout + result.stderr)
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


def execute_publication(launch: Launch, client: CoreClient) -> int:
    """Sanitize and compiler-attest one private workflow package."""
    claim = _claim_or_skip(client.claim_publication)
    if claim is None:
        return 0
    job = claim.get("job")
    hydration = claim.get("hydration")
    if not isinstance(job, dict) or not isinstance(hydration, dict):
        raise ContractError("invalid publication claim")
    provider = str(job.get("agent"))
    if provider not in ADAPTERS or hydration.get("provider") != provider:
        raise ContractError("invalid publication provider")
    root = Path(
        tempfile.mkdtemp(
            prefix="oci-publication-", dir=os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner")
        )
    )
    os.chmod(root, 0o700)
    package, output = root / "source", root / "public"
    package.mkdir()
    try:
        filename = str(job.get("source_filename") or "outcome.yml")
        if Path(filename).name != filename:
            raise ContractError("invalid publication source filename")
        (package / filename).write_text(str(job.get("content") or ""), encoding="utf-8")
        files = job.get("files") or {}
        if not isinstance(files, dict) or len(files) > 500:
            raise ContractError("invalid publication files")
        for name, encoded in files.items():
            path = package / str(name)
            if not path.resolve().is_relative_to(package.resolve()) or private_path(
                path.relative_to(package)
            ):
                raise ContractError("invalid publication file path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded, validate=True))

        def hydrate_and_run(active_provider: str, active_hydration: dict, active_model):
            pseudo_claim = SimpleNamespace(
                auth_json=active_hydration.get("auth_json"),
                oauth_token=active_hydration.get("oauth_token"),
                api_key=active_hydration.get("api_key"),
            )
            env = ADAPTERS[active_provider].hydrate(pseudo_claim, root, safe_env(root))
            previous = {key: os.environ.get(key) for key in env}
            os.environ.update(env)
            try:
                return prepare_publication(
                    package / filename,
                    output,
                    agent=active_provider,
                    model=active_model,
                    sensitive_terms=list(job.get("sensitive_terms") or []),
                    container_isolated=True,
                )
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        try:
            result = hydrate_and_run(provider, hydration, job.get("model"))
        except ExecutionError as exc:
            # Publication has no per-workflow declared fallback (unlike
            # workflow execution) -- it's a platform action tied to the
            # requesting user's own connection, not a workflow's agents
            # spec -- so a usage-limit hit always retries once on Claude.
            if provider == "claude" or not _is_usage_limit_error(exc):
                raise
            fallback = client.publication_agent_fallback(str(claim.get("completion_token")))
            provider = "claude"
            shutil.rmtree(output, ignore_errors=True)
            result = hydrate_and_run(
                provider, {"provider": "claude", "oauth_token": fallback["credential"]}, None
            )
        public_files: dict[str, str] = {}
        for path in sorted(output.rglob("*")):
            if (
                not path.is_file()
                or path.name == filename
                or path in {output / REQUIREMENTS, output / REPORT}
            ):
                continue
            public_files[path.relative_to(output).as_posix()] = base64.b64encode(
                path.read_bytes()
            ).decode()
        content = (output / filename).read_text(encoding="utf-8")
        client.complete_publication(
            str(claim.get("completion_token")),
            {
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "files": public_files,
                "package_sha256": result["package_digest"],
                "compiler_version": result["compiler_version"],
                "requirements": result["requirements"],
                "replacement_report": result["replacement_report"],
            },
        )
        return 0
    except Exception as exc:
        with suppress(CoreError):
            client.fail(str(claim.get("completion_token")), workflow_failure_category(exc), False)
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
        return execute_publication(launch, client)
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
