from __future__ import annotations

from pathlib import Path

from ..models import ContractError, ExecutionClaim


class OpenCodeAdapter:
    def hydrate(self, claim: ExecutionClaim, root: Path, env: dict[str, str]) -> dict[str, str]:
        if not claim.api_key:
            raise ContractError("missing OpenRouter API key")
        home = root / "opencode"
        home.mkdir(mode=0o700)
        return {
            **env,
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "OPENROUTER_API_KEY": claim.api_key,
        }

    def credential_update(self, claim: ExecutionClaim, root: Path) -> dict[str, object] | None:
        return None
