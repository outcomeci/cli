"""1Password references in the local Vault.

A local entry can hold `op://<vault>/<item>/<field>` references in place of
secret values. `oci workflow run` resolves them with a 1Password service
account from `OP_SERVICE_ACCOUNT_TOKEN`, through the 1Password SDK when it is
installed (`pip install outcomeci-cli[1password]`) and otherwise through the
`op` command-line tool. The workspace Vault resolves the same references on
the server with the workspace's service account.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any, TypeVar

from outcomeci.runtime.process import ExecutionError

TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
# `op://vault/item[/section]/field`, optionally `?ssh-format=openssh` to take an
# SSH Key item's private key in OpenSSH form instead of the SDK's PKCS#8.
REFERENCE_PATTERN = re.compile(
    r"^op://[^/?\n]+/[^/?\n]+(/[^/?\n]+)?/[^/?\n]+(\?ssh-format=(openssh|pkcs8))?$"
)
INTEGRATION_NAME = "OutcomeCI CLI"
INTEGRATION_VERSION = "1.0.0"
# Waits between attempts when 1Password cannot be reached: about ten seconds
# in all, so a blip never fails a run.
RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)

T = TypeVar("T")


def _unavailable(exc: Exception) -> bool:
    """Whether a failure is 1Password being unreachable rather than a
    reference or token problem, which a retry cannot fix."""
    if isinstance(exc, ExecutionError):
        return False
    text = str(exc).casefold()
    return "token" not in text and "not found" not in text and "no such" not in text


def _with_retries(attempt: Callable[[], T]) -> T:
    for delay in (*RETRY_DELAYS, None):
        try:
            return attempt()
        except Exception as exc:
            if delay is None or not _unavailable(exc):
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def is_reference(value: str) -> bool:
    return bool(REFERENCE_PATTERN.match(value))


def parse_refs(given: list[str], *, default_field: str | None) -> dict[str, str]:
    """`--ref` values as secret field -> reference. A bare `op://` reference
    fills the type's default field; `field=op://...` names one."""
    references: dict[str, str] = {}
    for item in given:
        if is_reference(item.strip()):
            field, reference = default_field or "", item
        else:
            field, _separator, reference = item.partition("=")
        reference = reference.strip()
        if not is_reference(reference):
            raise ExecutionError(
                f"--ref takes a 1Password reference such as op://vault/item/field, not {item!r}"
            )
        if not field:
            raise ExecutionError(f"--ref {reference} needs a field name, as field={reference}")
        references[field.strip()] = reference
    return references


def linked_references(value: Any) -> dict[str, str] | None:
    """A local entry's references by secret field, or None for a stored value."""
    if isinstance(value, dict) and isinstance(value.get("link"), dict):
        references = value["link"].get("references")
        if isinstance(references, dict):
            return {str(k): str(v) for k, v in references.items()}
    return None


async def _resolve_with_sdk(token: str, references: list[str]) -> dict[str, str]:
    from onepassword import Client  # type: ignore[import-not-found]

    client = await Client.authenticate(
        auth=token, integration_name=INTEGRATION_NAME, integration_version=INTEGRATION_VERSION
    )
    values = {}
    for reference in references:
        try:
            values[reference] = await client.secrets.resolve(reference)
        except Exception as exc:
            # The secrets API rejects titles with spaces and never reads file
            # attachments; walk the item through the items API instead.
            values[reference] = await read_item(client, reference, exc)
    return values


async def read_item(client: Any, reference: str, cause: Exception) -> str:
    """A field's value, or an attached file's text, found by walking the item."""
    vault_name, item_name, section, name = parse_reference(reference)
    try:
        vault = _named(await client.vaults.list(), vault_name)
        item = await client.items.get(
            vault.id, _named(await client.items.list(vault.id), item_name).id
        )
        try:
            return locate_field(item, section, name).value or ""
        except ExecutionError:
            pass
        section_ids = {
            s.id
            for s in item.sections
            if section == s.id or s.title.casefold() == section.casefold()
        }
        files = [
            f
            for f in item.files
            if f.attributes.name == name and (section is None or f.section_id in section_ids)
        ]
        if len(files) != 1:
            raise ExecutionError(f"1Password could not resolve {reference}") from cause
        return (await client.items.files.read(vault.id, item.id, files[0].attributes)).decode()
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError(f"1Password could not resolve {reference}") from exc


def _resolve_with_op(token: str, references: list[str]) -> dict[str, str]:
    op = shutil.which("op")
    if op is None:
        raise ExecutionError(
            "resolving 1Password references needs the 1Password SDK "
            "(`pip install outcomeci-cli[1password]`) or the `op` command-line tool"
        )
    values = {}
    for reference in references:
        result = subprocess.run(
            [op, "read", "--no-newline", reference],
            env={**os.environ, TOKEN_ENV: token},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ExecutionError(f"1Password could not resolve {reference}")
        values[reference] = result.stdout
    return values


def resolve(references: list[str]) -> dict[str, str]:
    """Each reference's value, by reference, through the service account in
    OP_SERVICE_ACCOUNT_TOKEN."""
    references = list(dict.fromkeys(references))
    if not references:
        return {}
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ExecutionError(
            f"the local Vault links to 1Password; set {TOKEN_ENV} to a service account token"
        )
    try:
        import onepassword  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return _resolve_with_op(token, references)
    try:
        return _with_retries(lambda: asyncio.run(_resolve_with_sdk(token, references)))
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError(f"1Password could not resolve a reference ({exc})") from exc


def parse_reference(reference: str) -> tuple[str, str, str | None, str]:
    """A reference's vault, item, optional section, and field, by name or id."""
    parts = reference.removeprefix("op://").partition("?")[0].split("/")
    return parts[0], parts[1], parts[2] if len(parts) == 4 else None, parts[-1]


def _named(candidates: list[Any], name: str) -> Any:
    by_id = [c for c in candidates if c.id == name]
    if by_id:
        return by_id[0]
    # 1Password matches titles without regard to case.
    by_title = [c for c in candidates if c.title.casefold() == name.casefold()]
    if len(by_title) != 1:
        raise ExecutionError(
            f"1Password has {'no' if not by_title else 'several'} items named {name}"
        )
    return by_title[0]


def locate_field(item: Any, section: str | None, field: str) -> Any:
    """The item field a reference names, by section and field id or title."""
    section_ids = None
    if section is not None:
        section_ids = {
            s.id
            for s in item.sections
            if section == s.id or s.title.casefold() == section.casefold()
        }
        if not section_ids:
            raise ExecutionError(f"1Password item {item.title} has no section {section}")
    found = [
        f
        for f in item.fields
        if (section_ids is None or f.section_id in section_ids)
        and (field == f.id or f.title.casefold() == field.casefold())
    ]
    if len(found) != 1:
        raise ExecutionError(
            f"1Password item {item.title} has {'no' if not found else 'several'} "
            f"fields named {field}"
        )
    return found[0]


async def _write_with_sdk(token: str, updates: dict[str, str]) -> None:
    from onepassword import Client  # type: ignore[import-not-found]

    client = await Client.authenticate(
        auth=token, integration_name=INTEGRATION_NAME, integration_version=INTEGRATION_VERSION
    )
    vaults = await client.vaults.list()
    for reference, value in updates.items():
        vault_name, item_name, section, field = parse_reference(reference)
        vault = _named(vaults, vault_name)
        item = await client.items.get(
            vault.id, _named(await client.items.list(vault.id), item_name).id
        )
        locate_field(item, section, field).value = value
        await client.items.put(item)


def write(updates: dict[str, str]) -> None:
    """Store each value in the field its reference names, through the SDK;
    the service account needs write access. The `op` tool is not used here,
    since it takes values on its command line."""
    if not updates:
        return
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ExecutionError(f"writing to 1Password needs {TOKEN_ENV}")
    try:
        import onepassword  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as exc:
        raise ExecutionError(
            "writing a rotated secret to 1Password needs the 1Password SDK "
            "(`pip install outcomeci-cli[1password]`)"
        ) from exc
    try:
        _with_retries(lambda: asyncio.run(_write_with_sdk(token, updates)))
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError(f"1Password could not update a reference ({exc})") from exc


def rotate_entry(value: dict[str, Any], secrets: dict[str, str]) -> None:
    """Write a linked entry's rotated secret fields to 1Password."""
    references = linked_references(value) or {}
    unlinked = sorted(set(secrets) - set(references))
    if unlinked:
        raise ExecutionError(f"{', '.join(unlinked)} not linked to 1Password on this entry")
    write({references[field]: secrets[field] for field in secrets})


def resolve_entry(value: dict[str, Any]) -> str | dict[str, Any]:
    """A linked local entry with its references replaced by their values."""
    references = linked_references(value)
    assert references is not None
    resolved = resolve(list(references.values()))
    secrets = {field: resolved[reference] for field, reference in references.items()}
    if "credential_type" not in value:
        return secrets["value"] if set(secrets) == {"value"} else secrets
    return {key: item for key, item in value.items() if key != "link"} | {"secrets": secrets}
