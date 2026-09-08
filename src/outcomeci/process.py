"""Small subprocess and GitHub boundaries for outcome execution."""
from __future__ import annotations

import base64
import os
import subprocess
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


def command(argv: list[str], *, cwd: Path, timeout: int = 300, input_text: str | None = None, env: dict[str, str] | None = None) -> Result:
    try:
        value = subprocess.run(argv, cwd=cwd, input=input_text, text=True, capture_output=True, timeout=timeout, env=env or os.environ.copy(), check=False)
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
            raise ExecutionError(f"{argv[0]} failed: {(result.stderr or result.stdout)[-1000:]}", True)
        return result.stdout.strip()

    def clone(self, repository: str, path: Path) -> None:
        result = command(["git", "clone", "--filter=blob:none", f"https://github.com/{repository}.git", str(path)], cwd=path.parent, env=self.env)
        if result.code:
            raise ExecutionError(f"git clone failed: {result.stderr[-1000:]}", True)


def invoke(agent: str, model: str | None, prompt: str, workspace: Path, timeout: int) -> str:
    secrets = {"GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    env = {key: value for key, value in os.environ.items() if key not in secrets}
    if agent == "codex":
        if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("CODEX_HOME"):
            raise ExecutionError("Codex needs OPENAI_API_KEY or an ephemeral CODEX_HOME")
        if os.environ.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        argv = ["codex", "exec", "--approve-for-me", "--skip-git-repo-check"]
        if model:
            argv += ["--model", model]
        argv += ["-"]
        input_text = prompt
    elif agent == "claude":
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        if not any(key in env for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")):
            raise ExecutionError("Claude needs ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        argv = ["claude", "--print", "--permission-mode", "acceptEdits"]
        if model:
            argv += ["--model", model]
        argv += [prompt]
        input_text = None
    else:
        raise ExecutionError(f"unsupported agent: {agent}")
    result = command(argv, cwd=workspace, timeout=timeout, input_text=input_text, env=env)
    if result.code:
        raise ExecutionError(f"{agent} failed with exit {result.code}: {(result.stderr or result.stdout)[-1000:]}", True)
    return result.stdout.strip()[-4000:]

