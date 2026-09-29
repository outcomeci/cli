"""Strict models for the Core/runner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

Provider = Literal["codex", "claude", "opencode"]


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
    mode: Literal["authorize", "workflow", "publication"]
    job_id: str
    bootstrap_token: str
    core_url: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Launch:
        mode = env.get("AGENT_RUNNER_MODE")
        if mode not in ("authorize", "workflow", "publication"):
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


class AgentLogin(Protocol):
    """The agent credential a runner installs: one field per provider."""

    auth_json: dict[str, Any] | None
    oauth_token: str | None
    api_key: str | None
