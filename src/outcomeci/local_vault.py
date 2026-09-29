"""Encrypted, offline credential storage for local OutcomeCI workflows."""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .process import ExecutionError
from .security import atomic_write_json

VAULT_FILE = Path(".outcomeci/vault.enc")
KEY_ENV = "OUTCOMECI_VAULT_KEY_FILE"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _config_home() -> Path:
    return Path(os.environ.get("OUTCOMECI_CONFIG_HOME", Path.home() / ".config/outcomeci"))


def _key_path(vault_id: str) -> Path:
    override = os.environ.get(KEY_ENV)
    return Path(override) if override else _config_home() / "vault-keys" / f"{vault_id}.key"


def _write_private(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


def _envelope(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / VAULT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError("local Vault is unavailable; run `oci vault local init`") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ExecutionError("local Vault format is unsupported")
    return value


def _key(vault_id: str) -> bytes:
    path = _key_path(vault_id)
    try:
        key = base64.urlsafe_b64decode(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ExecutionError(f"local Vault key is unavailable at {path}") from exc
    if len(key) != 32:
        raise ExecutionError("local Vault key is invalid")
    return key


def _save(root: Path, vault_id: str, key: bytes, payload: dict[str, Any]) -> None:
    nonce = secrets.token_bytes(12)
    aad = f"outcomeci-local-vault/v1:{vault_id}".encode()
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    atomic_write_json(
        root / VAULT_FILE,
        {
            "version": 1,
            "vault_id": vault_id,
            "algorithm": "AES-256-GCM",
            "nonce": base64.urlsafe_b64encode(nonce).decode(),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode(),
        },
        sort_keys=False,
        mode=stat.S_IRUSR | stat.S_IWUSR,
    )


def _load(root: Path) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    envelope = _envelope(root)
    vault_id, key = str(envelope["vault_id"]), _key(str(envelope["vault_id"]))
    try:
        plaintext = AESGCM(key).decrypt(
            base64.urlsafe_b64decode(envelope["nonce"]),
            base64.urlsafe_b64decode(envelope["ciphertext"]),
            f"outcomeci-local-vault/v1:{vault_id}".encode(),
        )
        payload = json.loads(plaintext)
    except Exception as exc:
        raise ExecutionError("local Vault could not be decrypted") from exc
    return envelope, key, payload


def initialize(root: Path) -> dict[str, Any]:
    path = root / VAULT_FILE
    if path.exists():
        raise ExecutionError("local Vault already exists")
    vault_id, key = secrets.token_hex(16), AESGCM.generate_key(bit_length=256)
    key_path = _key_path(vault_id)
    _write_private(key_path, base64.urlsafe_b64encode(key))
    _save(root, vault_id, key, {"entries": {}, "created_at": _now()})
    ignore = root / ".gitignore"
    content = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ".outcomeci/vault.enc" not in content.splitlines():
        ignore.write_text(
            content.rstrip() + "\n.outcomeci/vault.enc\n.outcomeci/.broker/\n", encoding="utf-8"
        )
    return {"initialized": True, "vault": str(path), "key_file": str(key_path)}


def is_valid_vault_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and ".." not in Path(path).parts


def put(root: Path, path: str, value: str) -> dict[str, Any]:
    if not is_valid_vault_path(path):
        raise ExecutionError("Vault path must be a relative logical path")
    envelope, key, payload = _load(root)
    existing = payload["entries"].get(path, {})
    payload["entries"][path] = {
        "value": value,
        "created_at": existing.get("created_at", _now()),
        "updated_at": _now(),
    }
    _save(root, envelope["vault_id"], key, payload)
    return {"path": path, "stored": True}


def list_entries(root: Path) -> dict[str, Any]:
    envelope, _key_value, payload = _load(root)
    return {
        "vault_id": envelope["vault_id"],
        "algorithm": envelope["algorithm"],
        "entries": [
            {"path": path, "created_at": item["created_at"], "updated_at": item["updated_at"]}
            for path, item in sorted(payload["entries"].items())
        ],
    }


def resolve(root: Path, reference: str) -> str | dict[str, Any]:
    if not reference.startswith("vault:"):
        raise ExecutionError("local Vault references must start with vault:")
    _envelope_value, _key_value, payload = _load(root)
    path = reference.removeprefix("vault:")
    try:
        value = str(payload["entries"][path]["value"])
    except KeyError as exc:
        raise ExecutionError(f"local Vault entry {path} was not found") from exc
    try:
        structured = json.loads(value)
    except json.JSONDecodeError:
        return value
    if (
        isinstance(structured, dict)
        and isinstance(structured.get("credential_type"), str)
        and isinstance(structured.get("secrets"), dict)
    ):
        return structured
    if isinstance(structured, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in structured.items()
    ):
        return structured
    return value


def rotate(root: Path, reference: str, secrets: dict[str, str]) -> None:
    """Replace secret fields of a typed credential, such as a rotated refresh token."""
    path = reference.removeprefix("vault:")
    current = resolve(root, reference)
    if not isinstance(current, dict) or not isinstance(current.get("secrets"), dict):
        raise ExecutionError(f"local Vault entry {path} is not a typed credential")
    updated = {**current, "secrets": {**current["secrets"], **secrets}}
    put(root, path, json.dumps(updated))
