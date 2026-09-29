from __future__ import annotations

import base64
import json
from pathlib import Path

import yaml

from outcomeci import cli
from outcomeci.cli import main
from outcomeci.config import compile_workflow
from outcomeci.repository import initialize


def test_init_writes_a_v1_workflow_that_validates(tmp_path: Path, capsys) -> None:
    assert main(["init", "--dir", str(tmp_path)]) == 0
    document = yaml.safe_load((tmp_path / "outcome.yml").read_text())
    assert document["apiVersion"] == "outcomeci.workflow/v1"
    assert (tmp_path / ".outcomeci/instructions/investigate.md").is_file()
    assert json.loads((tmp_path / ".outcomeci/request.json").read_text())["repo"]["owner"]
    assert not (tmp_path / ".agents").exists()
    assert main(["validate", "--dir", str(tmp_path)]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_workflow_get_writes_content_and_support_files(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_workflow",
        lambda workspace_id, workflow_id: {
            "workflow_id": workflow_id,
            "revision": 12,
            "content_sha256": "deadbeef",
            "content": "apiVersion: outcomeci.workflow/v1alpha1\n",
            "files": {
                ".outcomeci/instructions/orchestrator.md": base64.b64encode(
                    b"Run the phases."
                ).decode()
            },
        },
    )
    output = tmp_path / "outcome.yml"
    assert (
        main(
            [
                "workflow",
                "get",
                "workflow_1",
                "--workspace-id",
                "workspace_1",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_text() == "apiVersion: outcomeci.workflow/v1alpha1\n"
    support_file = tmp_path / ".outcomeci/instructions/orchestrator.md"
    assert support_file.read_text() == "Run the phases."
    result = json.loads(capsys.readouterr().out)
    assert result["revision"] == 12
    assert result["support_files_written"] == [str(support_file)]


def test_workflow_get_rejects_a_support_file_path_outside_outcomeci(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(
        cli,
        "get_workflow",
        lambda workspace_id, workflow_id: {
            "workflow_id": workflow_id,
            "revision": 1,
            "content_sha256": "deadbeef",
            "content": "apiVersion: outcomeci.workflow/v1alpha1\n",
            "files": {"../escape.md": base64.b64encode(b"x").decode()},
        },
    )
    output = tmp_path / "outcome.yml"
    assert (
        main(
            [
                "workflow",
                "get",
                "workflow_1",
                "--workspace-id",
                "workspace_1",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert "workflow support file path is invalid" in capsys.readouterr().err


def test_integration_dry_run_and_doctor_are_machine_readable(tmp_path: Path, capsys) -> None:
    initialize(tmp_path)
    phase = ["--phase", "investigate", "--workspace", str(tmp_path)]
    assert main(["integration", "dry-run", *phase]) == 0
    assert json.loads(capsys.readouterr().out)["requests_executed"] is False
    main(["integration", "doctor", "--workspace", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert report["credentials_exposed"] is False


def test_local_vault_commands_are_offline_and_never_print_values(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["vault", "local", "init", "--workspace", str(tmp_path)]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "vault",
                "local",
                "put",
                "linear/api_key",
                "--value",
                "top-secret",
                "--workspace",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert "top-secret" not in capsys.readouterr().out
    assert main(["vault", "local", "list", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "linear/api_key" in output
    assert "top-secret" not in output


def test_instruction_content_is_part_of_revision(tmp_path: Path) -> None:
    main(["init", "--dir", str(tmp_path)])
    first = compile_workflow(tmp_path / "outcome.yml")["workflow_revision"]
    instruction = tmp_path / ".outcomeci/instructions/investigate.md"
    instruction.write_text(instruction.read_text() + "\nAdditional policy.\n")
    assert compile_workflow(tmp_path / "outcome.yml")["workflow_revision"] != first


def test_slack_setup_passes_the_request_url_and_events(tmp_path: Path, monkeypatch, capsys) -> None:
    calls = []

    def fake_setup(workspace, **options):
        calls.append(options)
        return {"configured": True}

    monkeypatch.setattr(cli, "setup_slack", fake_setup)
    url = "https://example.com/v1/webhooks/route/token"
    base = ["integration", "slack", "setup", "--workspace", str(tmp_path)]

    assert main(base) == 0
    assert main([*base, "--request-url", url, "--event", "mention"]) == 0

    assert [(call["request_url"], call["events"]) for call in calls] == [
        (None, ["mention", "dm"]),
        (url, ["mention"]),
    ]


def test_local_vault_put_drops_the_newline_a_pipe_adds(tmp_path: Path, monkeypatch) -> None:
    import io

    from outcomeci import local_vault

    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["vault", "local", "init", "--workspace", str(tmp_path)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("ghp-token\n"))
    assert (
        main(["vault", "local", "put", "github", "--value-stdin", "--workspace", str(tmp_path)])
        == 0
    )
    assert local_vault.resolve(tmp_path, "vault:github") == "ghp-token"
