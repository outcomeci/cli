from __future__ import annotations

import json
import os
from pathlib import Path

from ..models import AgentLogin, ContractError

# Every Codex credential-file writer in cloud_runner (this adapter and the
# other two claim protocols in main.py) needs this same one-line config
# enabling file-based auth storage, so it lives in one place.
FILE_AUTH_CONFIG = 'cli_auth_credentials_store = "file"\n'


class CodexAdapter:
    def hydrate(self, claim: AgentLogin, root: Path, env: dict[str, str]) -> dict[str, str]:
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
            stream.write(FILE_AUTH_CONFIG)
        return {**env, "CODEX_HOME": str(home)}
