import io

from outcomeci import cli


def test_vault_put_does_not_print_secret(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("top-secret\n"))
    monkeypatch.setattr(cli, "vault_request", lambda workspace, operation, **values: seen.update(workspace=workspace, operation=operation, **values) or {"id": "entry-1", "path": "providers/openai"})
    assert cli.main(["vault", "put", "providers/openai", "--workspace", "workspace_1", "--value-stdin"]) == 0
    assert seen["value"] == "top-secret"
    assert "top-secret" not in capsys.readouterr().out


def test_vault_list_is_provider_neutral(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "vault_request", lambda workspace, operation, **values: {"entries": [], "workflows": []})
    assert cli.main(["vault", "list", "--workspace", "workspace_1"]) == 0
    assert '"entries": []' in capsys.readouterr().out
