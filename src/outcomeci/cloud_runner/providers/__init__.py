from outcomeci.cloud_runner.providers.claude import ClaudeAdapter
from outcomeci.cloud_runner.providers.codex import CodexAdapter
from outcomeci.cloud_runner.providers.opencode import OpenCodeAdapter

ADAPTERS = {"codex": CodexAdapter(), "claude": ClaudeAdapter(), "opencode": OpenCodeAdapter()}

__all__ = ["ADAPTERS", "ClaudeAdapter", "CodexAdapter", "OpenCodeAdapter"]
