from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import yaml
from lowered import compile_file
from test_integrations import workflow

from outcomeci.broker.executor import IntegrationExecutor, local_credential_resolver
from outcomeci.vault.local import initialize, list_entries, put, resolve


def test_local_vault_encrypts_values_and_lists_only_metadata(tmp_path: Path, monkeypatch) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(config_home))
    result = initialize(tmp_path)
    key_path = Path(result["key_file"])
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    put(tmp_path, "linear/api_key", "top-secret")
    encrypted = (tmp_path / ".outcomeci/vault.enc").read_text()
    assert "top-secret" not in encrypted
    assert "top-secret" not in json.dumps(list_entries(tmp_path))
    assert resolve(tmp_path, "vault:linear/api_key") == "top-secret"
    assert ".outcomeci/vault.enc" in (tmp_path / ".gitignore").read_text().splitlines()


def test_local_vault_resolves_structured_credentials_for_executor(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    initialize(tmp_path)
    put(tmp_path, "tickets/auth", json.dumps({"username": "izzy", "password": "secret"}))
    config = workflow(tmp_path)
    value = yaml.safe_load(config.read_text())
    auth = value["spec"]["connections"]["tickets"]["auth"]
    auth.clear()
    auth.update(
        {
            "connector": "tickets",
            "credential": "vault:tickets/auth",
            "accepts": [{"kind": "basic", "credential": ["username", "password"]}],
        }
    )
    config.write_text(yaml.safe_dump(value, sort_keys=False))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        return httpx.Response(201, json={"id": "T-1"})

    executor = IntegrationExecutor(
        compile_file(config),
        resolver=local_credential_resolver(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    result = executor.execute("tickets.create", {"title": "Test"}, step="intake")
    assert seen["authorization"].startswith("Basic ")
    assert "secret" not in json.dumps(result)
