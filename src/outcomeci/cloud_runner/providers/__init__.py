from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpenCodeAdapter

ADAPTERS = {"codex": CodexAdapter(), "claude": ClaudeAdapter(), "opencode": OpenCodeAdapter()}

__all__ = ["ADAPTERS", "ClaudeAdapter", "CodexAdapter", "OpenCodeAdapter"]
