from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from outcomeci import cli
from outcomeci.runtime.process import ExecutionError
from outcomeci.vault import onepassword
from outcomeci.vault.local import initialize, put, resolve


def test_parse_refs_fills_the_default_field_or_takes_named_fields() -> None:
    assert onepassword.parse_refs(["op://Eng/GitHub/token"], default_field="value") == {
        "value": "op://Eng/GitHub/token"
    }
    assert onepassword.parse_refs(
        ["username=op://Eng/DB/username", "password= op://Eng/DB/password "],
        default_field=None,
    ) == {"username": "op://Eng/DB/username", "password": "op://Eng/DB/password"}
    with pytest.raises(ExecutionError, match="needs a field name"):
        onepassword.parse_refs(["op://Eng/DB/password"], default_field=None)
    with pytest.raises(ExecutionError, match="1Password reference"):
        onepassword.parse_refs(["ghp_secret"], default_field="value")


def test_local_vault_resolves_linked_entries_at_run_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    initialize(tmp_path)
    seen = []

    def fake_resolve(references):
        seen.append(references)
        return {ref: f"value-of-{ref}" for ref in references}

    monkeypatch.setattr(onepassword, "resolve", fake_resolve)
    assert (
        cli.main(
            [
                "vault",
                "local",
                "put",
                "github",
                "--ref",
                "op://Eng/GitHub/token",
                "--dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "vault",
                "local",
                "put",
                "db",
                "--credential-type",
                "basic",
                "--ref",
                "username=op://Eng/DB/username",
                "--ref",
                "password=op://Eng/DB/password",
                "--dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert resolve(tmp_path, "vault:github") == "value-of-op://Eng/GitHub/token"
    assert resolve(tmp_path, "vault:db") == {
        "credential_type": "basic",
        "configuration": {},
        "secrets": {
            "username": "value-of-op://Eng/DB/username",
            "password": "value-of-op://Eng/DB/password",
        },
    }
    assert seen == [["op://Eng/GitHub/token"], ["op://Eng/DB/username", "op://Eng/DB/password"]]


def test_local_linked_entry_needs_a_service_account_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    initialize(tmp_path)
    put(
        tmp_path,
        "github",
        json.dumps({"link": {"backend": "1password", "references": {"value": "op://A/B/c"}}}),
    )
    with pytest.raises(ExecutionError, match="OP_SERVICE_ACCOUNT_TOKEN"):
        resolve(tmp_path, "vault:github")


def test_vault_put_ref_links_instead_of_storing(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: (
            seen.update(workspace=workspace, operation=operation, **values)
            or {"id": "entry-1", "path": "slack/app", "kind": "linked"}
        ),
    )
    assert (
        cli.main(
            [
                "vault",
                "put",
                "slack/app",
                "--workspace-id",
                "workspace_1",
                "--provider",
                "slack",
                "--credential-type",
                "oauth2",
                "--client-id",
                "abc",
                "--grant-type",
                "refresh_token",
                "--ref",
                "op://Eng/Slack/client secret",
                "--ref",
                "refresh_token=op://Eng/Slack/refresh token",
            ]
        )
        == 0
    )
    assert seen["operation"] == "put_linked"
    assert seen["credential"] == {
        "credential_type": "oauth2",
        "configuration": {"client_id": "abc", "grant_type": "refresh_token"},
        "link": {
            "backend": "1password",
            "references": {
                "client_secret": "op://Eng/Slack/client secret",
                "refresh_token": "op://Eng/Slack/refresh token",
            },
        },
    }
    assert '"kind": "linked"' in capsys.readouterr().out


def test_vault_put_ref_rejects_a_value_too(monkeypatch) -> None:
    monkeypatch.setattr(cli, "vault_request", lambda *a, **k: {})
    assert (
        cli.main(
            ["vault", "put", "github", "--workspace-id", "w", "--ref", "op://A/B/c", "--value", "x"]
        )
        != 0
    )


def test_vault_connect_sends_the_token_from_stdin(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("ops_token\n"))
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: (
            seen.update(workspace=workspace, operation=operation, **values)
            or {"id": "entry-1", "path": "system/1password-service-account"}
        ),
    )
    assert cli.main(["vault", "connect", "1password", "--workspace-id", "w", "--token-stdin"]) == 0
    assert seen == {"workspace": "w", "operation": "connect_1password", "token": "ops_token"}
    assert "ops_token" not in capsys.readouterr().out


def test_vault_disconnect_revokes_the_connection(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: seen.update(
            workspace=workspace, operation=operation, **values
        ),
    )
    assert cli.main(["vault", "disconnect", "1password", "--workspace-id", "w"]) == 0
    assert seen == {"workspace": "w", "operation": "disconnect_1password"}
    assert '"ok": true' in capsys.readouterr().out


def test_local_rotation_of_a_linked_entry_writes_to_1password(tmp_path: Path, monkeypatch) -> None:
    from outcomeci.vault.local import rotate

    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    initialize(tmp_path)
    put(
        tmp_path,
        "slack/app",
        json.dumps(
            {
                "credential_type": "oauth2",
                "configuration": {"client_id": "abc", "grant_type": "refresh_token"},
                "link": {
                    "backend": "1password",
                    "references": {
                        "client_secret": "op://Eng/Slack/client secret",
                        "refresh_token": "op://Eng/Slack/refresh token",
                    },
                },
            }
        ),
    )
    written = {}
    monkeypatch.setattr(onepassword, "write", written.update)
    rotate(tmp_path, "vault:slack/app", {"refresh_token": "rt-new"})
    assert written == {"op://Eng/Slack/refresh token": "rt-new"}
    assert "rt-new" not in (tmp_path / ".outcomeci/vault.enc").read_text()
    with pytest.raises(ExecutionError, match="not linked"):
        rotate(tmp_path, "vault:slack/app", {"private_key": "x"})


def test_locate_field_and_parse_reference() -> None:
    from types import SimpleNamespace

    assert onepassword.parse_reference("op://Eng/Slack/OAuth/refresh token") == (
        "Eng",
        "Slack",
        "OAuth",
        "refresh token",
    )
    item = SimpleNamespace(
        title="Slack",
        sections=[SimpleNamespace(id="s1", title="OAuth")],
        fields=[
            SimpleNamespace(id="f1", title="refresh token", section_id="s1", value="old"),
            SimpleNamespace(id="f2", title="refresh token", section_id=None, value="old"),
        ],
    )
    assert onepassword.locate_field(item, "OAuth", "refresh token").id == "f1"
    assert onepassword.locate_field(item, None, "f2").id == "f2"
    with pytest.raises(ExecutionError, match="several fields"):
        onepassword.locate_field(item, None, "refresh token")


def test_write_needs_the_token_and_sdk(monkeypatch) -> None:
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    with pytest.raises(ExecutionError, match="OP_SERVICE_ACCOUNT_TOKEN"):
        onepassword.write({"op://A/B/c": "x"})


def test_openssh_private_keys_sign_like_pem_ones() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from outcomeci.broker.auth import sign_rs256

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    openssh = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    pkcs8 = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert openssh.startswith("-----BEGIN OPENSSH")
    claims = {"iss": "me", "exp": 4102444800}
    assert sign_rs256(claims, openssh) == sign_rs256(claims, pkcs8)


def test_references_may_carry_an_ssh_format_and_files_resolve_from_items() -> None:
    import asyncio
    from types import SimpleNamespace

    assert onepassword.is_reference("op://Eng/Deploy/private key?ssh-format=openssh")
    assert onepassword.parse_reference("op://Eng/Deploy/private key?ssh-format=openssh")[3] == (
        "private key"
    )

    async def vaults():
        return [SimpleNamespace(id="v1", title="Eng")]

    async def items(vault_id):
        return [SimpleNamespace(id="i1", title="SA")]

    async def get(vault_id, item_id):
        return SimpleNamespace(
            id="i1",
            title="SA",
            sections=[],
            fields=[SimpleNamespace(id="x", title="api token", section_id=None, value="tok")],
            files=[
                SimpleNamespace(
                    attributes=SimpleNamespace(name="key.json", id="f1"), section_id="s"
                )
            ],
        )

    async def read(vault_id, item_id, attributes):
        return b"{}"

    client = SimpleNamespace(
        vaults=SimpleNamespace(list=vaults),
        items=SimpleNamespace(list=items, get=get, files=SimpleNamespace(read=read)),
    )
    assert (
        asyncio.run(onepassword.read_item(client, "op://Eng/SA/key.json", RuntimeError())) == "{}"
    )
    assert (
        asyncio.run(onepassword.read_item(client, "op://Eng/sa/API token", RuntimeError())) == "tok"
    )
    with pytest.raises(ExecutionError, match="could not resolve"):
        asyncio.run(onepassword.read_item(client, "op://Eng/SA/nope.json", RuntimeError()))


def test_unreachable_1password_is_retried_but_bad_references_are_not(monkeypatch) -> None:
    monkeypatch.setattr(onepassword, "RETRY_DELAYS", (0, 0))
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("dial tcp: connection refused")
        return "ok"

    assert onepassword._with_retries(flaky) == "ok"
    assert len(calls) == 3

    def bad_token() -> str:
        calls.append(1)
        raise RuntimeError("invalid service account token")

    calls.clear()
    with pytest.raises(RuntimeError):
        onepassword._with_retries(bad_token)
    assert len(calls) == 1
