from outcomeci.cloud_runner.providers.base import ConnectorAdapter


class OpenCodeAdapter(ConnectorAdapter):
    def __init__(self):
        super().__init__("opencode")
