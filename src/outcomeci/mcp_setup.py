"""Register the hosted OutcomeCI MCP server with the coding agents installed here.

Each agent has its own `mcp add` command and its own way to sign in, so this
module only decides what to run: it finds the agents on PATH, skips any that
already know the server, and runs the agent's own commands. Sign-in always
happens in the browser through the agent; no OutcomeCI credential is written
into an agent's configuration.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_API_URL = "https://api.outcomeci.com"
DEFAULT_NAME = "outcomeci"


@dataclass(frozen=True)
class Agent:
    """One coding agent: its executable and how it registers and signs in."""

    id: str
    label: str
    executable: str
    # Names under which the agent may already reach OutcomeCI, besides the
    # name being registered. Claude Code lists claude.ai connectors this way.
    existing_names: tuple[str, ...] = ()

    def add(self, name: str, url: str) -> list[str]:
        if self.id == "claude":
            return [
                self.executable,
                "mcp",
                "add",
                "--transport",
                "http",
                "--scope",
                "user",
                name,
                url,
            ]
        if self.id == "codex":
            return [self.executable, "mcp", "add", name, "--url", url]
        return [self.executable, "mcp", "add", name, "--url", url]

    def login(self, name: str) -> list[str] | None:
        """The command that signs in, when the agent has one outside a session."""
        if self.id == "opencode":
            return [self.executable, "mcp", "auth", name]
        # Codex signs in as part of `codex mcp add`; Claude Code signs in from
        # its /mcp menu on first use.
        return None

    def sign_in_hint(self, name: str) -> str:
        if self.id == "claude":
            return f"Start Claude Code, run /mcp, choose {name} and sign in"
        if self.id == "codex":
            return f"Run `codex mcp login {name}` if sign-in did not finish"
        return f"Run `opencode mcp auth {name}` to sign in"


AGENTS = (
    Agent("claude", "Claude Code", "claude", existing_names=("claude.ai OutcomeCI",)),
    Agent("codex", "Codex", "codex"),
    Agent("opencode", "OpenCode", "opencode"),
)
AGENT_IDS = tuple(agent.id for agent in AGENTS)

Run = Callable[..., subprocess.CompletedProcess]


def mcp_url(api_url: str) -> str:
    return f"{api_url.rstrip('/')}/v1/mcp"


def default_api_url(credentials_path: Path | None = None) -> str:
    """OUTCOMECI_API_URL, else the API `oci auth login` signed in to, else production."""
    if os.environ.get("OUTCOMECI_API_URL"):
        return os.environ["OUTCOMECI_API_URL"]
    if credentials_path is not None and credentials_path.is_file():
        try:
            stored = json.loads(credentials_path.read_text(encoding="utf-8")).get("api_url")
        except (OSError, ValueError, AttributeError):
            stored = None
        if isinstance(stored, str) and stored.startswith(("https://", "http://")):
            return stored
    return DEFAULT_API_URL


def _opencode_config_paths() -> list[Path]:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    return [base / "opencode.json", base / "opencode.jsonc"]


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas, as OpenCode writes it, made plain JSON."""
    text = re.sub(
        r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S
    )
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _opencode_has(name: str) -> bool:
    for path in _opencode_config_paths():
        if not path.is_file():
            continue
        try:
            config = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        servers = config.get("mcp") if isinstance(config, dict) else None
        if isinstance(servers, dict) and name in servers:
            return True
    return False


def configured_name(agent: Agent, name: str, run: Run) -> str | None:
    """The name under which the agent already reaches the server, if any."""
    if agent.id == "opencode":
        return name if _opencode_has(name) else None
    for candidate in (name, *agent.existing_names):
        found = run(
            [agent.executable, "mcp", "get", candidate],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if found.returncode == 0:
            return candidate
    return None


def init(
    *,
    api_url: str,
    name: str = DEFAULT_NAME,
    agents: Sequence[str] = (),
    dry_run: bool = False,
    login: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    run: Run = subprocess.run,
    report: Callable[[str], None] = lambda line: None,
) -> dict[str, Any]:
    """Register the server with each installed agent, or with `agents` only.

    The agents' own commands run attached to this terminal, because Codex and
    OpenCode open a browser and wait for the sign-in to finish.
    """
    url = mcp_url(api_url)
    wanted = [agent for agent in AGENTS if not agents or agent.id in agents]
    results: list[dict[str, Any]] = []
    for agent in wanted:
        executable = which(agent.executable)
        if executable is None:
            results.append({"agent": agent.id, "status": "not_installed"})
            if agents:
                report(f"{agent.label}: `{agent.executable}` is not on PATH")
            continue
        agent = Agent(agent.id, agent.label, executable, agent.existing_names)
        existing = configured_name(agent, name, run)
        if existing is not None:
            report(f"{agent.label}: already set up as {existing}")
            results.append({"agent": agent.id, "status": "already_configured", "name": existing})
            continue
        command = agent.add(name, url)
        if dry_run:
            results.append({"agent": agent.id, "status": "would_add", "command": command})
            continue
        report(f"{agent.label}: adding {name} ({url})")
        if run(command).returncode != 0:
            results.append({"agent": agent.id, "status": "failed", "command": command})
            continue
        entry: dict[str, Any] = {"agent": agent.id, "status": "added", "name": name}
        sign_in = agent.login(name) if login else None
        if sign_in is not None and run(sign_in).returncode == 0:
            entry["signed_in"] = True
        else:
            entry["next_step"] = agent.sign_in_hint(name)
        results.append(entry)
    return {"url": url, "dry_run": dry_run, "agents": results}
