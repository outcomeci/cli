import io

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
                "--workflow",
                "workflow_1",
                "--value-stdin",
            ]
        )
        == 0
    )
    assert seen["operation"] == "put_credential"
    assert seen["provider"] == "slack"
    assert seen["credential_type"] == "auth_header"
    assert seen["value"] == "xoxb-private"
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


def test_typed_auth_header_uses_credential_endpoint_and_safe_defaults(monkeypatch) -> None:
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
        credential_type="auth_header",
        value="xoxb-private",
        workflow_ids=["workflow_1"],
    )
    assert seen["path"].endswith("/vault/credentials")
    assert seen["body"]["service"] == "slack"
    assert seen["body"]["configuration"] == {
        "header_name": "Authorization",
        "scheme": "Bearer",
    }
    assert seen["body"]["secrets"] == {"value": "xoxb-private"}


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
    assert seen["secrets"] == {"client_secret": "one", "refresh_token": "two"}
