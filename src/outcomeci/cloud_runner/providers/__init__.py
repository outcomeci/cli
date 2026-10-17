from outcomeci_connectors.agents import agent_providers

from outcomeci.cloud_runner.providers.base import ConnectorAdapter
from outcomeci.cloud_runner.providers.claude import ClaudeAdapter
from outcomeci.cloud_runner.providers.codex import CodexAdapter
from outcomeci.cloud_runner.providers.opencode import OpenCodeAdapter

ADAPTERS = {name: ConnectorAdapter(name) for name in agent_providers()}

__all__ = ["ADAPTERS", "ClaudeAdapter", "CodexAdapter", "OpenCodeAdapter"]
