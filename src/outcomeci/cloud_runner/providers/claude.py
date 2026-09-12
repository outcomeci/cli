from __future__ import annotations

from pathlib import Path

from ..models import ContractError, ExecutionClaim


class ClaudeAdapter:
    def hydrate(self, claim: ExecutionClaim, root: Path, env: dict[str, str]) -> dict[str, str]:
        if not claim.oauth_token:
            raise ContractError("missing Claude credential")
        return {**env, "CLAUDE_CODE_OAUTH_TOKEN": claim.oauth_token}

    def credential_update(self, claim: ExecutionClaim, root: Path) -> dict[str, object] | None:
        return None
