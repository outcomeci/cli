from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS = {"codex": CodexAdapter(), "claude": ClaudeAdapter()}

__all__ = ["ADAPTERS", "ClaudeAdapter", "CodexAdapter"]
