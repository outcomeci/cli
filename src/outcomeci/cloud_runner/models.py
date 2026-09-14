"""Strict models for the Core/runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

Provider = Literal["codex", "claude", "opencode"]
OUTCOME_PHASES = (
    "intake",
    "repository_selection",
    "specify",
    "plan",
    "tasks",
    "implementation",
    "validation",
    "publication",
)


class ContractError(ValueError):
    pass


def string(value: Any, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractError(f"invalid {field}")
    return value


def https(value: Any, field: str) -> str:
    result = string(value, field)
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ContractError(f"invalid {field}")
    return result.rstrip("/")


def core_url(value: Any, allow_insecure: bool) -> str:
    result = string(value, "Core URL")
    parsed = urlparse(result)
    local_hosts = {"localhost", "127.0.0.1", "host.docker.internal"}
    valid_https = parsed.scheme == "https"
    valid_local_http = allow_insecure and parsed.scheme == "http" and parsed.hostname in local_hosts
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or not (valid_https or valid_local_http)
    ):
        raise ContractError("invalid Core URL")
    return result.rstrip("/")


def provider(value: Any) -> Provider:
    if value not in ("codex", "claude", "opencode"):
        raise ContractError("invalid provider")
    return value


def command(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ContractError("invalid command")
    return tuple(string(item, "command argument", 8192) for item in value)


@dataclass(frozen=True)
class Launch:
    mode: Literal["authorize", "outcome"]
    job_id: str
    bootstrap_token: str
    core_url: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Launch:
        mode = env.get("AGENT_RUNNER_MODE")
        if mode not in ("authorize", "outcome"):
            raise ContractError("invalid runner mode")
        return cls(
            mode,
            string(env.get("AGENT_JOB_ID"), "job id", 256),
            string(env.get("AGENT_BOOTSTRAP_TOKEN"), "bootstrap token", 8192),
            core_url(env.get("OUTCOMECI_API_URL"), env.get("AGENT_ALLOW_INSECURE_CORE") == "true"),
        )


@dataclass(frozen=True)
class AuthorizationClaim:
    provider: Provider
    command: tuple[str, ...]
    session_token: str
    expires_at: str

    @classmethod
    def parse(cls, raw: Any) -> AuthorizationClaim:
        if not isinstance(raw, dict):
            raise ContractError("invalid authorization claim")
        return cls(
            provider(raw.get("provider")),
            command(raw.get("command")),
            string(raw.get("session_token"), "session token", 8192),
            string(raw.get("expires_at"), "expiry", 128),
        )


@dataclass(frozen=True)
class ExecutionClaim:
    provider: Provider
    lease_id: str
    lease_version: int
    lease_expires_at: str
    completion_token: str
    core_job_token: str
    job: dict[str, Any]
    command: tuple[str, ...]
    github_token: str
    auth_json: dict[str, Any] | None
    oauth_token: str | None
    api_key: str | None
    timeout_seconds: int
    outcome_run: Any = None
    outcome: Any = None

    @classmethod
    def parse(cls, raw: Any) -> ExecutionClaim:
        if not isinstance(raw, dict) or not isinstance(raw.get("job"), dict):
            raise ContractError("invalid execution claim")
        hydration = raw.get("hydration")
        if not isinstance(hydration, dict):
            raise ContractError("invalid hydration")
        selected = provider(hydration.get("provider"))
        job = raw["job"]
        if job.get("agent") != selected:
            raise ContractError("cross-provider claim")
        outcome_run = raw.get("outcome_run")
        if outcome_run is not None or job.get("kind") != "outcome" or "outcome_run" in job:
            raise ContractError("invalid Outcome job binding")
        string(job.get("job_id"), "job id")
        repositories = job.get("repositories")
        if not isinstance(repositories, list) or len(repositories) > 11:
            raise ContractError("invalid job repositories")
        normalized = [string(item, "job repository", 511).lower() for item in repositories]
        if len(set(normalized)) != len(normalized):
            raise ContractError("duplicate job repository")
        version, timeout = raw.get("credential_version"), raw.get("timeout_seconds", 3600)
        if (
            not isinstance(version, int)
            or version < 1
            or not isinstance(timeout, int)
            or not 1 <= timeout <= 21600
        ):
            raise ContractError("invalid lease")
        auth_json = hydration.get("auth_json") if selected == "codex" else None
        oauth_token = hydration.get("oauth_token") if selected == "claude" else None
        api_key = hydration.get("api_key") if selected == "opencode" else None
        if selected == "codex" and not isinstance(auth_json, dict):
            raise ContractError("invalid Codex hydration")
        if selected == "claude":
            oauth_token = string(oauth_token, "Claude credential", 32768)
        if selected == "opencode":
            api_key = string(api_key, "OpenRouter API key", 32768)
            model = job.get("model")
            if not isinstance(model, str) or not model.startswith("openrouter/"):
                raise ContractError("invalid OpenCode model")
        outcome = raw.get("outcome")
        if job.get("kind") == "outcome" and not isinstance(outcome, dict):
            raise ContractError("invalid outcome binding")
        github_token = raw.get("github_token")
        if github_token is None:
            github_token = ""
        if not isinstance(github_token, str) or len(github_token) > 32768:
            raise ContractError("invalid GitHub token")
        return cls(
            selected,
            string(raw.get("lease_id"), "lease id", 256),
            version,
            string(raw.get("lease_expires_at"), "lease expiry", 128),
            string(raw.get("completion_token"), "completion token", 8192),
            string(raw.get("core_job_token"), "Core job token", 8192),
            job,
            command(raw.get("command")),
            github_token,
            auth_json,
            oauth_token,
            api_key,
            timeout,
            outcome_run,
            outcome,
        )
