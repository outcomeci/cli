"""Shared exclusions for broker-private and credential-bearing files."""

from pathlib import Path


def private_path(path: str | Path) -> bool:
    value = Path(path)
    return bool({".broker", ".capability"}.intersection(value.parts)) or (
        value.name == "vault.enc" or value.name == ".env" or value.name.startswith(".env.")
    )
