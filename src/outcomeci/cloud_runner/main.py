"""Entrypoint for private authorization and execution tasks."""
from __future__ import annotations
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
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

def reconcile_failure(client: CoreClient, claim: object, category: str, retryable: bool) -> None:
    """Best-effort terminal reconciliation after an execution has been claimed."""
    try:
        client.fail(
            getattr(claim, "completion_token"),
            category,
            retryable,
            getattr(claim, "lease_id"),
        )
    except CoreError:
        # Preserve the original failure. Core's lease expiry/reconciler remains
        # the final fallback if the terminal call itself is unavailable.
        pass

def safe_verification(provider: str, output: str) -> tuple[str, str | None] | None:
    output = ANSI_ESCAPE.sub("", output)
    url = URL.search(output)
    code = USER_CODE.search(output)
    if not url or (provider == "codex" and not code):
        return None
    return url.group(0).rstrip(".,)"), code.group(0) if code else None

def safe_env(root: Path, github_token: str | None = None, core_job_token: str | None = None) -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update({"HOME": str(root), "TMPDIR": str(root / "tmp")})
    if github_token:
        env["GH_TOKEN"] = github_token
        env["GITHUB_TOKEN"] = github_token
    if core_job_token:
        env["OUTCOMECI_API_KEY"] = core_job_token
    return env

def classify_failure(output: str, *, authorization: bool, cancelled: bool = False) -> tuple[str, bool]:
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
    if any(term in lowered for term in ("authentication", "unauthorized", "oauth", "token expired", "login required", "failed to refresh token", "refresh token was already used")):
        return "provider_auth_rejected", False
    return "agent_process_failed", False

def authorize(launch: Launch, client: CoreClient) -> int:
    claim = client.claim_authorization()
    root = Path(tempfile.mkdtemp(prefix="oci-agent-auth-", dir=os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner")))
    os.chmod(root, 0o700)
    try:
        (root / "tmp").mkdir(mode=0o700)
        env = safe_env(root)
        if claim.provider == "codex":
            codex_home = root / "codex"
            codex_home.mkdir(mode=0o700)
            (codex_home / "config.toml").write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
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
                client.verification(claim.session_token, verification[0], verification[1], claim.expires_at)
                published = True
        next_response_poll = 0.0
        def response_input() -> str | None:
            nonlocal next_response_poll
            if claim.provider != "claude" or not published or time.monotonic() < next_response_poll:
                return None
            next_response_poll = time.monotonic() + 2.0
            return client.authorization_response(claim.session_token)
        result = run(claim.command, cwd=root, env=env, timeout=900, on_output=progress, terminal=True, input_provider=response_input)
        if result.returncode:
            category, retryable = classify_failure(result.stdout + result.stderr, authorization=True, cancelled=result.returncode < 0)
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
    root = Path(tempfile.mkdtemp(prefix="oci-outcome-", dir=os.environ.get("AGENT_PRIVATE_ROOT", "/home/runner")))
    os.chmod(root, 0o700)
    try:
        (root / "tmp").mkdir(mode=0o700)
        env = adapter.hydrate(claim, root, safe_env(root, claim.github_token, claim.core_job_token))
        env.update({
            "OUTCOMECI_API_URL": launch.core_url,
            "OUTCOMECI_JOB_ID": claim.job["job_id"],
            "OUTCOMECI_JOB_TOKEN": claim.core_job_token,
        })
        workspace = Path(os.environ.get("AGENT_WORK_ROOT", "/workspace"))
        if not workspace.is_dir():
            raise ContractError("runner workspace is unavailable")
        (workspace / "outcome-claim.json").write_text(json.dumps(claim.outcome,separators=(",",":")),encoding="utf-8")
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
        result = run(claim.command, cwd=workspace, env=env, timeout=claim.timeout_seconds, on_tick=tick)
        try:
            lines=[line for line in result.stdout.splitlines() if line.strip()]
            outcome_result=json.loads(lines[-1]) if lines else None
            if not isinstance(outcome_result,dict) or outcome_result.get("status") not in {"awaiting_confirmation","ready_for_implementation","completed"}: raise ContractError("invalid outcome result")
            client.complete(claim.completion_token,{"result":outcome_result})
            return 0
        except (ContractError, json.JSONDecodeError):
            category, retryable = classify_failure(result.stdout + result.stderr, authorization=False, cancelled=result.returncode < 0)
            client.fail(claim.completion_token, category, retryable, claim.lease_id)
            return result.returncode or 1
    except TimeoutError:
        reconcile_failure(client, claim, "agent_timeout", True)
        raise
    except CoreError as error:
        reconcile_failure(client, claim, error.category, error.retryable)
        raise
    except (ContractError, KeyError, json.JSONDecodeError):
        reconcile_failure(client, claim, "invalid_job", False)
        raise
    except Exception:
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
        return authorize(launch, client) if launch.mode == "authorize" else execute(launch, client)
    except TimeoutError:
        category, retryable = "agent_timeout", True
    except CoreError as error:
        category, retryable = error.category, error.retryable
    except (ContractError, KeyError, json.JSONDecodeError):
        category, retryable = "invalid_job", False
    except Exception as error:
        category, retryable = "internal_failure", True
        exception_type = type(error).__name__
    payload = {"event": "outcome_runner_failed", "category": category, "retryable": retryable}
    if category == "internal_failure":
        payload["exception_type"] = exception_type
    print(json.dumps(payload), file=sys.stderr)
    return 1
