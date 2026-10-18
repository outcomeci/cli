from __future__ import annotations

from pathlib import Path
from typing import Protocol

from outcomeci.cloud_runner.models import AgentLogin


class ProviderAdapter(Protocol):
    def hydrate(self, claim: AgentLogin, root: Path, env: dict[str, str]) -> dict[str, str]: ...


class ConnectorAdapter:
    """Hydrate a connector's declared credential inside the runtime-owned root."""

    def __init__(self, provider: str):
        self.provider = provider

    def hydrate(self, claim: AgentLogin, root: Path, env: dict[str, str]) -> dict[str, str]:
        import json
        import os

        from outcomeci_connectors.agents import agent_provider

        from outcomeci.cloud_runner.models import ContractError

        descriptor = agent_provider(self.provider)
        value = getattr(claim, descriptor.credential_secret)
        if not value:
            raise ContractError(f"missing {self.provider} credential")
        result = dict(env)
        home = root / (descriptor.home_directory or self.provider)
        if descriptor.home_directory:
            home.mkdir(mode=0o700)
        if descriptor.credential_file:
            files = {
                descriptor.credential_file: json.dumps(value, separators=(",", ":")),
                **descriptor.config_files,
            }
            for name, content in files.items():
                fd = os.open(home / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
            result[descriptor.environment_variable] = str(home)
        else:
            result[descriptor.environment_variable] = value
        if descriptor.isolated_home:
            result.update(
                HOME=str(home),
                XDG_DATA_HOME=str(home / ".local" / "share"),
                XDG_CONFIG_HOME=str(home / ".config"),
            )
        return result
