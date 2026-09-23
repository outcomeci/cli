"""Small subprocess and GitHub boundaries for outcome execution."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class ExecutionError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str


def command(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 300,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> Result:
    try:
        value = subprocess.run(
            argv,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env or os.environ.copy(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError(f"command timed out after {timeout}s: {argv[0]}", True) from exc
    return Result(value.returncode, value.stdout[-20000:], value.stderr[-20000:])


class GitHub:
    def __init__(self, token: str):
        if not token:
            raise ExecutionError("GITHUB_TOKEN is required")
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self.env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def run(self, argv: list[str], cwd: Path, timeout: int = 300) -> str:
        result = command(argv, cwd=cwd, timeout=timeout, env=self.env)
        if result.code:
            raise ExecutionError(
                f"{argv[0]} failed: {(result.stderr or result.stdout)[-1000:]}", True
            )
        return result.stdout.strip()

    def clone(self, repository: str, path: Path) -> None:
        result = command(
            [
                "git",
                "clone",
                "--filter=blob:none",
                f"https://github.com/{repository}.git",
                str(path),
            ],
            cwd=path.parent,
            env=self.env,
        )
        if result.code:
            raise ExecutionError(f"git clone failed: {result.stderr[-1000:]}", True)


def _authorize_agent(
    agent: str, model: str | None, env: dict[str, str], *, allow_local_auth: bool
) -> None:
    """Propagate this agent's credential env vars into `env` in place.

    Raises if the agent can't authenticate, or (opencode only) its model
    selection is invalid. Shared by invoke() and invoke_conversation(),
    which otherwise diverge on everything else about how they run an agent.
    """
    if agent == "codex":
        if os.environ.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        if (
            not allow_local_auth
            and not os.environ.get("OPENAI_API_KEY")
            and not os.environ.get("CODEX_HOME")
        ):
            raise ExecutionError("Codex needs OPENAI_API_KEY or an ephemeral CODEX_HOME")
    elif agent == "claude":
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        if not allow_local_auth and not any(
            key in env for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
        ):
            raise ExecutionError("Claude needs ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
    elif agent == "opencode":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ExecutionError("OpenCode needs an injected OPENROUTER_API_KEY")
        if not model or not model.startswith("openrouter/"):
            raise ExecutionError("OpenCode needs an explicit openrouter/<model> selection")
        env["OPENROUTER_API_KEY"] = api_key
    else:
        raise ExecutionError(f"unsupported agent: {agent}")


def invoke(
    agent: str,
    model: str | None,
    prompt: str,
    workspace: Path,
    timeout: int,
    *,
    allow_local_auth: bool = False,
    extra_env: dict[str, str] | None = None,
    writable_paths: list[Path] | None = None,
    excluded_env: set[str] | None = None,
    read_only: bool = False,
    container_isolated: bool = False,
) -> str:
    secrets = {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENROUTER_API_KEY",
        "OUTCOMECI_VAULT_KEY_FILE",
    }
    secrets.update(excluded_env or set())
    env = {key: value for key, value in os.environ.items() if key not in secrets}
    env.update(extra_env or {})
    if read_only:
        env = {key: value for key, value in env.items() if not key.startswith("OUTCOMECI_")}
    _authorize_agent(agent, model, env, allow_local_auth=allow_local_auth)
    if agent == "codex":
        argv = ["codex", "exec", "--approve-for-me", "--skip-git-repo-check"]
        if container_isolated:
            # OutcomeCI Cloud runs inside a dedicated, least-privilege Fargate
            # task. Codex's nested Linux sandbox is unavailable there; the
            # container and capability broker are the security boundary.
            argv = [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ]
        if read_only:
            argv = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        if model:
            argv += ["--model", model]
        argv += ["-"]
        input_text = prompt
    elif agent == "claude":
        argv = ["claude", "--print", "--permission-mode", "acceptEdits"]
        if container_isolated:
            # Same reasoning as Codex above: OutcomeCI Cloud's Fargate task is
            # the security boundary, and this runs fully non-interactively.
            # acceptEdits only auto-approves file edits, not arbitrary tool
            # calls -- the integration broker CLI still hits an approval
            # prompt that can never be answered here, and Claude reports the
            # call as rejected rather than actually invoking it.
            argv = ["claude", "--print", "--dangerously-skip-permissions"]
        if read_only:
            argv = ["claude", "--print", "--tools", "", "--permission-mode", "default"]
        if model:
            argv += ["--model", model]
        argv += [prompt]
        input_text = None
    else:  # opencode, already validated above
        argv = ["opencode", "run", "--pure", "--auto", "--format", "json", "--model", model, prompt]
        input_text = None
    if writable_paths is not None and not container_isolated:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise ExecutionError("bubblewrap is required for secure local agent execution")
        wrapper = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--tmpfs",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
        ]
        # Expose runtimes, not the host filesystem. In particular, a workflow
        # cannot read another repository's .env or the user's cloud/SSH keys.
        runtime_paths = {
            Path(value)
            for value in (
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/etc",
                "/opt",
                "/home/linuxbrew",
            )
        }
        runtime_paths.update(
            {Path(sys.prefix), Path(sys.base_prefix), Path(__file__).resolve().parents[2]}
        )
        runtime_paths.update(
            {
                Path.home() / ".local" / "lib",
                Path.home() / ".local" / "bin",
                Path.home() / ".local" / "share" / "claude",
            }
        )
        for runtime_path in sorted(runtime_paths):
            if runtime_path.exists():
                wrapper += ["--ro-bind", str(runtime_path), str(runtime_path)]
        # Hosts may use a resolver symlink into /run. Expose only its target,
        # not /run (which contains host sockets and other private state).
        resolver = Path("/etc/resolv.conf")
        if resolver.is_symlink() and resolver.resolve().is_file():
            target = resolver.resolve()
            # Bind at the first link destination: the host may have another
            # symlink there, whose intermediate directory is intentionally absent.
            destination = Path(os.readlink(resolver))
            if not destination.is_absolute():
                destination = resolver.parent / destination
            wrapper += ["--ro-bind", str(target), str(destination)]
        wrapper += ["--ro-bind", str(workspace), str(workspace)]
        slack_home = Path.home() / ".slack"
        if slack_home.exists():
            wrapper += ["--tmpfs", str(slack_home)]
        for agent_home in (
            Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            if agent == "codex"
            else Path.home() / ".claude",
        ):
            if agent_home.exists():
                wrapper += ["--bind", str(agent_home), str(agent_home)]
        vault_key = os.environ.get("OUTCOMECI_VAULT_KEY_FILE")
        if vault_key and Path(vault_key).is_file():
            wrapper += ["--ro-bind", "/dev/null", str(Path(vault_key).resolve())]
        for writable in writable_paths:
            wrapper += ["--bind", str(writable), str(writable)]
        capability_socket = (extra_env or {}).get("OUTCOMECI_CAPABILITY_SOCKET")
        if capability_socket:
            socket_directory = str(Path(capability_socket).parent)
            wrapper += ["--ro-bind", socket_directory, socket_directory]
        # Apply masks after writable binds: no artifact directory can reveal
        # the broker journal, Vault ciphertext/key or cloud authentication.
        config_home = Path(
            os.environ.get("OUTCOMECI_CONFIG_HOME", Path.home() / ".config/outcomeci")
        )
        for private in (
            config_home,
            workspace / ".outcomeci" / "vault.enc",
            workspace / ".outcomeci" / ".broker",
        ):
            if private.is_dir():
                wrapper += ["--tmpfs", str(private)]
            elif private.is_file():
                wrapper += ["--ro-bind", "/dev/null", str(private)]
        outcomes = workspace / ".outcomeci" / "outcomes"
        if outcomes.exists():
            for journal in outcomes.glob("*/.broker"):
                wrapper += ["--tmpfs", str(journal)]
        for directory, subdirectories, files in os.walk(workspace):
            subdirectories[:] = [
                name
                for name in subdirectories
                if name not in {"node_modules", ".git", ".venv", "transcripts", ".worktrees"}
            ]
            for filename in files:
                if filename == ".env" or filename.startswith(".env."):
                    wrapper += ["--ro-bind", "/dev/null", str(Path(directory) / filename)]
        wrapper += ["--"]
        argv = [*wrapper, *argv]
    result = command(argv, cwd=workspace, timeout=timeout, input_text=input_text, env=env)
    if result.code:
        raise ExecutionError(
            f"{agent} failed with exit {result.code}: {(result.stderr or result.stdout)[-1000:]}",
            True,
        )
    return result.stdout.strip()[-4000:]


def invoke_conversation(
    agent: str,
    session_id: str | None,
    model: str | None,
    prompt: str,
    workspace: Path,
    timeout: int,
    *,
    allow_local_auth: bool = False,
) -> str:
    """Run a read-only turn, resuming the outcome session when available."""
    secrets = {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENROUTER_API_KEY",
    }
    env = {key: value for key, value in os.environ.items() if key not in secrets}
    _authorize_agent(agent, model, env, allow_local_auth=allow_local_auth)
    if agent == "codex":
        base = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        if model:
            base += ["--model", model]
        argv = [*base, "resume", session_id, "-"] if session_id else [*base, "-"]
        input_text = prompt
    elif agent == "claude":
        argv = [
            "claude",
            "--print",
            "--permission-mode",
            "manual",
            "--allowedTools",
            "Read,Grep,Glob",
        ]
        if session_id:
            argv += ["--resume", session_id, "--fork-session"]
        if model:
            argv += ["--model", model]
        argv += [prompt]
        input_text = None
    else:  # opencode, already validated above
        argv = ["opencode", "run", "--pure", "--format", "json", "--model", model, prompt]
        input_text = None
    result = command(argv, cwd=workspace, timeout=timeout, input_text=input_text, env=env)
    if (
        agent == "codex"
        and session_id
        and result.code
        and "active writer" in (result.stderr or result.stdout)
    ):
        # Codex cannot concurrently resume a session that is still open in an
        # interactive client. A fresh read-only turn can recover its context
        # from the durable outcome artifacts named in the prompt.
        result = command([*base, "-"], cwd=workspace, timeout=timeout, input_text=prompt, env=env)
    if result.code:
        raise ExecutionError(
            f"{agent} conversation failed with exit {result.code}: {(result.stderr or result.stdout)[-1000:]}",
            True,
        )
    return result.stdout.strip()[-12000:]
