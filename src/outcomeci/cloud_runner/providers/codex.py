from __future__ import annotations

import json
import os
from pathlib import Path

from ..models import ContractError, ExecutionClaim


class CodexAdapter:
    def hydrate(self, claim: ExecutionClaim, root: Path, env: dict[str, str]) -> dict[str, str]:
        if claim.auth_json is None:
            raise ContractError("missing Codex credential")
        home = root / "codex"
        home.mkdir(mode=0o700)
        auth = home / "auth.json"
        descriptor = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(claim.auth_json, stream, separators=(",", ":"))
        config = home / "config.toml"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write('cli_auth_credentials_store = "file"\n')
        return {**env, "CODEX_HOME": str(home)}

    def credential_update(self, claim: ExecutionClaim, root: Path) -> dict[str, object] | None:
        auth = root / "codex" / "auth.json"
        raw = auth.read_bytes()
        if len(raw) > 1_048_576:
            raise ContractError("invalid Codex credential")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ContractError("invalid Codex credential")
        return {
            "provider": "codex",
            "auth_json": parsed,
            "expected_credential_version": claim.lease_version,
        }
