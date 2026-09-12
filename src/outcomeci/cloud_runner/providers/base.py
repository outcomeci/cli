from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import ExecutionClaim


class ProviderAdapter(Protocol):
    def hydrate(self, claim: ExecutionClaim, root: Path, env: dict[str, str]) -> dict[str, str]: ...
    def credential_update(self, claim: ExecutionClaim, root: Path) -> dict[str, object] | None: ...
