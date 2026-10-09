import io

import pytest

from outcomeci import cli, cloud


def test_vault_put_does_not_print_secret(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("top-secret\n"))
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: (
            seen.update(workspace=workspace, operation=operation, **values)
            or {"id": "entry-1", "path": "providers/openai"}
        ),
    )
    assert (
        cli.main(
            ["vault", "put", "providers/openai", "--workspace-id", "workspace_1", "--value-stdin"]
        )
        == 0
    )
    assert seen["value"] == "top-secret"
    assert "top-secret" not in capsys.readouterr().out


def test_vault_list_is_provider_neutral(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: {"entries": [], "workflows": []},
    )
    assert cli.main(["vault", "list", "--workspace-id", "workspace_1"]) == 0
    assert '"entries": []' in capsys.readouterr().out


def test_vault_put_typed_credential_keeps_value_private(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("xoxb-private\n"))
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: (
            seen.update(workspace=workspace, operation=operation, **values)
            or {"id": "entry-1", "path": "slack/bot-token"}
        ),
    )
    assert (
        cli.main(
            [
                "vault",
                "put",
                "slack/bot-token",
                "--workspace-id",
                "workspace_1",
                "--provider",
                "slack",
                "--credential-type",
                "auth_header",
                "--workflow-id",
                "workflow_1",
                "--value-stdin",
            ]
        )
        == 0
    )
    assert seen["operation"] == "put_credential"
    assert seen["provider"] == "slack"
    assert seen["credential"] == {
        "credential_type": "auth_header",
        "configuration": {},
        "secrets": {"value": "xoxb-private"},
    }
    assert "xoxb-private" not in capsys.readouterr().out


def test_vault_put_rejects_incomplete_typed_contract(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("private\n"))
    assert (
        cli.main(
            [
                "vault",
                "put",
                "slack/bot-token",
                "--workspace-id",
                "workspace_1",
                "--provider",
                "slack",
                "--value-stdin",
            ]
        )
        == 2
    )
    assert "--provider and --credential-type" in capsys.readouterr().err


def test_typed_credential_is_posted_to_the_credential_endpoint(monkeypatch) -> None:
    seen = {}

    def request(path, *, method="GET", body=None):
        seen.update(path=path, method=method, body=body)
        return 201, {"id": "entry-1"}

    monkeypatch.setattr(cloud, "_authorized_request", request)
    cloud.vault_request(
        "workspace_1",
        "put_credential",
        path="slack/bot-token",
        display_name="Slack bot token",
        provider="slack",
        credential={
            "credential_type": "auth_header",
            "configuration": {},
            "secrets": {"value": "xoxb-private"},
        },
        workflow_ids=["workflow_1"],
    )
    assert seen["path"].endswith("/vault/credentials")
    assert seen["body"] == {
        "path": "slack/bot-token",
        "display_name": "Slack bot token",
        "service": "slack",
        "credential_type": "auth_header",
        "configuration": {},
        "secrets": {"value": "xoxb-private"},
        "workflow_ids": ["workflow_1"],
    }


def _put(monkeypatch, stdin: str, *arguments: str) -> dict:
    seen: dict = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: seen.update(values) or {"id": "entry"},
    )
    assert cli.main(["vault", "put", *arguments, "--workspace-id", "workspace_1"]) == 0
    return seen["credential"]


@pytest.mark.parametrize("kind", ["oauth2", "oidc", "jwt_bearer"])
def test_typed_credential_scopes_use_the_vault_field_for_the_kind(monkeypatch, kind) -> None:
    credential = _put(
        monkeypatch,
        "private-secret\n",
        "service/auth",
        "--provider",
        "service",
        "--credential-type",
        kind,
        *(["--issuer", "service@example.test"] if kind == "jwt_bearer" else ["--client-id", "cid"]),
        "--scope",
        "read",
        "--scope",
        "write",
        "--value-stdin",
    )
    if kind == "jwt_bearer":
        assert credential["configuration"] == {
            "issuer": "service@example.test",
            "scope": "read write",
        }
    else:
        assert credential["configuration"] == {"client_id": "cid", "scopes": ["read", "write"]}


def test_basic_and_app_installation_credentials(monkeypatch) -> None:
    basic = _put(
        monkeypatch,
        '{"username": "izzy", "password": "pw"}',
        "jira/login",
        "--provider",
        "jira",
        "--credential-type",
        "basic",
        "--secrets-json-stdin",
    )
    assert basic == {
        "credential_type": "basic",
        "configuration": {},
        "secrets": {"username": "izzy", "password": "pw"},
    }
    app = _put(
        monkeypatch,
        "-----BEGIN PRIVATE KEY-----\n",
        "github/app",
        "--provider",
        "github",
        "--credential-type",
        "app_installation",
        "--app-id",
        "123",
        "--installation-id",
        "456",
        "--value-stdin",
    )
    assert app["configuration"] == {"app_id": "123", "installation_id": "456"}
    assert app["secrets"] == {"private_key": "-----BEGIN PRIVATE KEY-----"}


def test_a_typed_credential_is_checked_before_it_is_sent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("key\n"))
    arguments = ["github/app", "--provider", "github", "--credential-type", "app_installation"]
    assert cli.main(["vault", "put", *arguments, "--workspace-id", "w", "--value-stdin"]) == 2
    assert "--app-id, --installation-id" in capsys.readouterr().err
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("key\n"))
    arguments = ["slack/token", "--provider", "slack", "--credential-type", "auth_header"]
    assert (
        cli.main(
            [
                "vault",
                "put",
                *arguments,
                "--token-url",
                "https://x",
                "--workspace-id",
                "w",
                "--value-stdin",
            ]
        )
        == 2
    )
    assert "--token-url does not apply to a auth_header credential" in capsys.readouterr().err


def test_the_local_vault_stores_typed_credentials_like_the_cloud(
    monkeypatch, tmp_path, capsys
) -> None:
    from outcomeci.local_vault import resolve

    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    assert cli.main(["vault", "local", "init", "--dir", str(tmp_path)]) == 0
    monkeypatch.setattr(
        cli.sys, "stdin", io.StringIO('{"client_secret": "cs", "refresh_token": "rt"}')
    )
    arguments = [
        "vault",
        "local",
        "put",
        "slack/app",
        "--credential-type",
        "oauth2",
        "--client-id",
        "cid",
        "--grant-type",
        "refresh_token",
        "--secrets-json-stdin",
        "--dir",
        str(tmp_path),
    ]
    assert cli.main(arguments) == 0
    assert "rt" not in capsys.readouterr().out.replace('"stored"', "")
    assert resolve(tmp_path, "vault:slack/app") == {
        "credential_type": "oauth2",
        "configuration": {"client_id": "cid", "grant_type": "refresh_token"},
        "secrets": {"client_secret": "cs", "refresh_token": "rt"},
    }


def test_a_local_rotation_replaces_only_the_rotated_field(monkeypatch, tmp_path) -> None:
    import json

    from outcomeci.integrations import local_credential_resolver
    from outcomeci.local_vault import initialize, put, resolve

    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    initialize(tmp_path)
    stored = {
        "credential_type": "oauth2",
        "configuration": {"client_id": "cid"},
        "secrets": {"client_secret": "cs", "refresh_token": "rt-1"},
    }
    put(tmp_path, "slack/app", json.dumps(stored))
    local_credential_resolver(tmp_path).rotate("vault:slack/app", {"refresh_token": "rt-2"})
    assert resolve(tmp_path, "vault:slack/app")["secrets"] == {
        "client_secret": "cs",
        "refresh_token": "rt-2",
    }


def test_typed_credential_rejects_secret_in_process_arguments(capsys) -> None:
    assert (
        cli.main(
            [
                "vault",
                "put",
                "slack/bot-token",
                "--workspace-id",
                "workspace_1",
                "--provider",
                "slack",
                "--credential-type",
                "auth_header",
                "--value",
                "private",
            ]
        )
        == 2
    )
    assert "supplied through stdin" in capsys.readouterr().err


def test_oauth_secret_object_is_read_from_stdin(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO('{"client_secret":"one","refresh_token":"two"}\n'),
    )
    monkeypatch.setattr(
        cli,
        "vault_request",
        lambda workspace, operation, **values: seen.update(values) or {"id": "entry"},
    )
    assert (
        cli.main(
            [
                "vault",
                "put",
                "linear/oauth",
                "--workspace-id",
                "workspace_1",
                "--provider",
                "linear",
                "--credential-type",
                "oauth2",
                "--token-url",
                "https://api.linear.app/oauth/token",
                "--client-id",
                "client",
                "--grant-type",
                "refresh_token",
                "--secrets-json-stdin",
            ]
        )
        == 0
    )
    assert seen["credential"]["secrets"] == {"client_secret": "one", "refresh_token": "two"}
