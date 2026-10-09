from __future__ import annotations

from pathlib import Path

from outcomeci.cloud_runner.models import AgentLogin, ContractError


class ClaudeAdapter:
    def hydrate(self, claim: AgentLogin, root: Path, env: dict[str, str]) -> dict[str, str]:
        if not claim.oauth_token:
            raise ContractError("missing Claude credential")
        return {**env, "CLAUDE_CODE_OAUTH_TOKEN": claim.oauth_token}
