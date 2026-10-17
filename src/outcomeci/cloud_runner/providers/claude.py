from outcomeci.cloud_runner.providers.base import ConnectorAdapter


class ClaudeAdapter(ConnectorAdapter):
    def __init__(self):
        super().__init__("claude")
