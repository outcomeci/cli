from outcomeci_connectors.agents import agent_provider

from outcomeci.cloud_runner.providers.base import ConnectorAdapter

FILE_AUTH_CONFIG = agent_provider("codex").config_files["config.toml"]


class CodexAdapter(ConnectorAdapter):
    def __init__(self):
        super().__init__("codex")
