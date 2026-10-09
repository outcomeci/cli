from __future__ import annotations

from pathlib import Path
from typing import Protocol

from outcomeci.cloud_runner.models import AgentLogin


class ProviderAdapter(Protocol):
    def hydrate(self, claim: AgentLogin, root: Path, env: dict[str, str]) -> dict[str, str]: ...
