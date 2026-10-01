"""Credential-free local checkouts of the GitHub repositories a step is granted.

An agent that may only reach a repository through the brokered GitHub API
spends its request budget reading files one at a time. Before the agent
starts, the runtime clones each repository its grants name, with the same
credential the connector uses, so the agent reads and searches the code
locally. Writes still go only through the broker: a checkout holds no
credential and cannot push.

A clone is shallow, bounded in time and size, and never fatal: one that
fails is recorded as a warning and the agent reads that repository through
the API instead. No clone counts against an api's `max_requests`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .auth import Authenticator
from .execution_events import event, safe_text
from .integrations import CredentialResolver
from .policy import _path_fields
from .process import ExecutionError, terminate_gracefully

CHECKOUT_TIMEOUT_SECONDS = 300
CHECKOUT_MAX_BYTES = 500 * 1024 * 1024
POLL_SECONDS = 0.5
GITHUB_API = "https://api.github.com"
GITHUB_GIT = "https://github.com/"
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REF = re.compile(r"[A-Za-z0-9_./-]{1,200}")
# Shorter values are not credentials, and would match unrelated bytes.
_MIN_SECRET = 8


class CheckoutError(ExecutionError):
    """A repository could not be checked out; the message is credential-free."""


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str
    ref: str | None
    capabilities: tuple[str, ...]
    connection: Mapping[str, Any]

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def github_url(owner: str, name: str) -> str:
    """The plain https URL a checkout's remote names."""
    return f"{GITHUB_GIT}{owner}/{name}.git"


# Where a clone fetches from. Tests point it at a local repository.
clone_url: Callable[[str, str], str] = github_url


def directory(root: Path) -> Path:
    """Where a run's checkouts live: in the agent's workspace, outside its artifacts."""
    return root / ".outcomeci" / "repos"


def remove(root: Path) -> None:
    shutil.rmtree(directory(root), ignore_errors=True)


def _safe_name(value: str) -> bool:
    return bool(_NAME.fullmatch(value)) and value not in {".", ".."}


def _safe_ref(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and _REF.fullmatch(value)
        and not value.startswith(("-", "/"))
        and ".." not in value
    ):
        return value
    return None


def granted(compiled: Mapping[str, Any], grants: list[dict[str, Any]] | None) -> list[Repository]:
    """The GitHub repositories these resolved grants name, once each.

    Only a grant with a resolved `repo` argument on a github.com api names one;
    a grant without a repository restriction gets no checkout."""
    spec = compiled["workflow"]["spec"]
    found: dict[tuple[str, str], Repository] = {}
    for grant in grants or []:
        repo = (grant.get("args") or {}).get("repo")
        fields = _path_fields(repo, ["owner", "name"]) if repo is not None else None
        if fields is None or not all(_safe_name(value) for value in fields.values()):
            continue
        capability = str(grant["capability"])
        integration = spec.get("integrations", {}).get(capability.partition(".")[0])
        if integration is None:
            continue
        connection = next(
            (item for item in spec["connections"] if item["ref"] == integration["connection"]),
            None,
        )
        if (
            connection is None
            or connection["auth"].get("connector") != "github"
            or connection["base_url"].rstrip("/") != GITHUB_API
        ):
            continue
        key = (fields["owner"].casefold(), fields["name"].casefold())
        ref = _safe_ref(repo.get("ref") or repo.get("branch")) if isinstance(repo, dict) else None
        previous = found.get(key)
        if previous is None:
            found[key] = Repository(fields["owner"], fields["name"], ref, (capability,), connection)
        elif capability not in previous.capabilities:
            found[key] = Repository(
                previous.owner,
                previous.name,
                previous.ref or ref,
                (*previous.capabilities, capability),
                previous.connection,
            )
    return list(found.values())


def _authorization(
    connection: Mapping[str, Any],
    resolver: CredentialResolver,
    authenticator: Authenticator,
    client: httpx.Client,
) -> tuple[str | None, list[str]]:
    """The header git authenticates with, and every secret it derives from.

    The connector's own authentication runs here, so a GitHub App credential is
    exchanged for an installation token exactly as an API call would be."""
    auth = connection["auth"]
    reference = auth.get("credential")
    if not reference:
        return None, []
    headers: dict[str, str] = {}
    sensitive = authenticator.apply(client, auth, resolver(reference), headers, {})
    value = next((item for key, item in headers.items() if key.lower() == "authorization"), "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() in {"bearer", "token"} and token:
        # Git over https takes a token as the password of a basic login.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        header = f"Authorization: basic {basic}"
        return header, [*sensitive, token, basic, header]
    if scheme.lower() == "basic" and token:
        header = f"Authorization: {value}"
        return header, [*sensitive, token, header]
    raise CheckoutError("the credential cannot authenticate a git clone")


def _environment(authorization: str | None) -> dict[str, str]:
    """The clone's environment: the credential as process-scoped git config,
    which git never writes to the repository, and no prompts or helpers."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    config = [("credential.helper", "")]
    if authorization is not None:
        config.append((f"http.{GITHUB_GIT}.extraheader", authorization))
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"})
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _size(path: Path) -> int:
    total = 0
    for directory_path, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory_path, name)).st_size
            except OSError:
                continue
    return total


def _redacted(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[credential withheld]")
    return safe_text(text, 500)


def leaked(checkout: Path, secrets: list[str]) -> list[str]:
    """Files in a checkout's git directory that hold any of `secrets`.

    Objects are the repository's own content as the server sent it; every other
    file (config, packed-refs, FETCH_HEAD, logs, hooks) is scanned."""
    needles = [secret.encode() for secret in secrets if len(secret) >= _MIN_SECRET]
    found = []
    git = checkout / ".git"
    for directory_path, subdirectories, files in os.walk(git):
        if Path(directory_path) == git:
            subdirectories[:] = [name for name in subdirectories if name != "objects"]
        for name in files:
            path = Path(directory_path) / name
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if any(needle in content for needle in needles):
                found.append(str(path.relative_to(checkout)))
    return found


def _git(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=_environment(None),
    )
    if result.returncode:
        raise CheckoutError(f"git {argv[0]} failed in the checkout")
    return result.stdout.strip()


def clone(
    repository: Repository,
    destination: Path,
    authorization: str | None,
    *,
    secrets: list[str],
    timeout: float = CHECKOUT_TIMEOUT_SECONDS,
    max_bytes: int = CHECKOUT_MAX_BYTES,
) -> dict[str, Any]:
    """Shallow-clone one repository to `destination`, leaving no credential in it.

    Raises CheckoutError, with `destination` removed, when the clone fails,
    takes longer than `timeout` seconds, grows past `max_bytes`, or a
    credential is found in the result."""
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    argv = ["git", "clone", "--quiet", "--depth", "1", "--no-tags", "--single-branch"]
    if repository.ref:
        argv += ["--branch", repository.ref]
    argv += ["--", clone_url(repository.owner, repository.name), str(destination)]
    try:
        with tempfile.TemporaryFile() as errors:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=destination.parent,
                    env=_environment(authorization),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                )
            except FileNotFoundError as exc:
                raise CheckoutError("git is not installed") from exc
            deadline = time.monotonic() + timeout
            while True:
                try:
                    code = process.wait(timeout=POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > deadline:
                    terminate_gracefully(process)
                    raise CheckoutError(f"the clone took longer than {int(timeout)}s")
                if _size(destination) > max_bytes:
                    terminate_gracefully(process)
                    raise CheckoutError(f"the repository is larger than {max_bytes} bytes")
            if code:
                errors.seek(0)
                detail = errors.read()[-2000:].decode(errors="replace").strip()
                raise CheckoutError(
                    f"git clone exited {code}"
                    + (f": {_redacted(detail, secrets)}" if detail else "")
                )
        size = _size(destination)
        if size > max_bytes:
            raise CheckoutError(f"the repository is larger than {max_bytes} bytes")
        commit = _git(["rev-parse", "HEAD"], destination)
        _git(
            ["remote", "set-url", "origin", github_url(repository.owner, repository.name)],
            destination,
        )
        if leaked(destination, secrets):
            raise CheckoutError("the checkout kept a credential and was discarded")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {"commit": commit, "bytes": size}


def prepare(
    root: Path,
    compiled: Mapping[str, Any],
    grants: list[dict[str, Any]] | None,
    resolver: CredentialResolver,
    *,
    step: str,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    authenticator: Authenticator | None = None,
) -> list[dict[str, Any]]:
    """Check out every repository `grants` name, replacing earlier checkouts.

    Returns one credential-free record per repository: its `path` and `commit`
    when checked out, or the `error` that left it to the API. Each is also sent
    to `event_sink` as an integration event."""
    remove(root)
    repositories = granted(compiled, grants)
    if not repositories:
        return []
    authenticator = authenticator or Authenticator(rotate=getattr(resolver, "rotate", None))
    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for repository in repositories:
            destination = directory(root) / repository.owner / repository.name
            record: dict[str, Any] = {
                "repo": repository.full_name,
                "ref": repository.ref,
                "capabilities": list(repository.capabilities),
            }
            secrets: list[str] = []
            try:
                authorization, secrets = _authorization(
                    repository.connection, resolver, authenticator, client
                )
                record.update(
                    clone(repository, destination, authorization, secrets=secrets),
                    path=str(destination),
                )
            except Exception as exc:
                record["error"] = _redacted(str(exc) or type(exc).__name__, secrets)
            records.append(record)
            if event_sink is None:
                continue
            detail = json.dumps(
                {
                    key: record[key]
                    for key in ("repo", "ref", "commit", "bytes")
                    if record.get(key) is not None
                },
                separators=(",", ":"),
            )
            endpoint = github_url(repository.owner, repository.name)
            if "error" in record:
                event_sink(
                    event(
                        "integration.failed",
                        step,
                        repository.capabilities[0],
                        f"Local checkout of {repository.full_name} failed; "
                        "the agent reads it through the API",
                        method="GET",
                        endpoint=endpoint,
                        reason=record["error"],
                        ok=False,
                        level="warning",
                        detail=detail,
                    )
                )
            else:
                event_sink(
                    event(
                        "integration.completed",
                        step,
                        repository.capabilities[0],
                        f"Checked out {repository.full_name} at {record['commit']}",
                        method="GET",
                        endpoint=endpoint,
                        ok=True,
                        detail=detail,
                    )
                )
    return records


def note(records: list[dict[str, Any]]) -> str:
    """What the agent is told about its checkouts, or nothing without one."""
    lines = []
    for record in records:
        if "path" not in record:
            continue
        branch = f"branch {record['ref']}" if record.get("ref") else "the default branch"
        lines.append(
            f"- {record['repo']}: {record['path']} ({branch} at commit {record['commit']}; "
            f"changes through {', '.join(record['capabilities'])})"
        )
    if not lines:
        return ""
    return (
        "Local checkouts: each repository below is a shallow snapshot, cloned for this step. "
        "Read and search it with local tools; local reads and searches are free and do not "
        "count against the API request budget. A checkout holds no credentials and cannot "
        "push: create branches, commits and pull requests only through the API capability "
        "named for its repository.\n" + "\n".join(lines)
    )
