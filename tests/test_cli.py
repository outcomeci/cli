from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

from outcomeci import cli
from outcomeci.cli import main
from outcomeci.workflow.compiler import compile_workflow
from outcomeci.workflow.scaffold import initialize


def test_init_writes_a_v1_workflow_that_validates(tmp_path: Path, capsys) -> None:
    assert main(["init", "--dir", str(tmp_path)]) == 0
    source = (tmp_path / "outcome.yml").read_text()
    document = yaml.safe_load(source)
    assert document["apiVersion"] == "outcomeci.workflow/v1"
    # The header's run command must run every step, or the first run stops
    # after one step with no way to continue it.
    assert "oci workflow run --payload .outcomeci/request.json --auto-continue" in source
    assert (tmp_path / ".outcomeci/instructions/investigate.md").is_file()
    assert json.loads((tmp_path / ".outcomeci/request.json").read_text())["repo"]["owner"]
    assert not (tmp_path / ".agents").exists()
    assert main(["validate", "--dir", str(tmp_path)]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_an_interrupted_run_exits_130_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.workflow_run, "run_local", interrupted)

    assert main(["workflow", "run", "--dir", str(tmp_path)]) == 130

    captured = capsys.readouterr()
    assert captured.err.strip() == "oci: interrupted"
    assert "Traceback" not in captured.err


def test_workflow_get_writes_content_and_support_files(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "get_workflow",
        lambda workspace_id, workflow_id: {
            "workflow_id": workflow_id,
            "revision": 12,
            "content_sha256": "deadbeef",
            "content": "apiVersion: outcomeci.workflow/v1\n",
            "files": {
                ".outcomeci/instructions/orchestrator.md": base64.b64encode(
                    b"Run the steps."
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
    assert output.read_text() == "apiVersion: outcomeci.workflow/v1\n"
    support_file = tmp_path / ".outcomeci/instructions/orchestrator.md"
    assert support_file.read_text() == "Run the steps."
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
            "content": "apiVersion: outcomeci.workflow/v1\n",
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
    step = ["--step", "investigate", "--dir", str(tmp_path)]
    assert main(["integration", "dry-run", *step]) == 0
    assert json.loads(capsys.readouterr().out)["requests_executed"] is False
    main(["integration", "doctor", "--dir", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert report["credentials_exposed"] is False


def test_local_vault_commands_are_offline_and_never_print_values(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["vault", "local", "init", "--dir", str(tmp_path)]) == 0
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
                "--dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert "top-secret" not in capsys.readouterr().out
    assert main(["vault", "local", "list", "--dir", str(tmp_path)]) == 0
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
    base = ["integration", "slack", "setup", "--dir", str(tmp_path)]

    assert main(base) == 0
    assert main([*base, "--request-url", url, "--event", "mention"]) == 0

    assert [(call["request_url"], call["events"]) for call in calls] == [
        (None, ["mention", "dm"]),
        (url, ["mention"]),
    ]


def test_local_vault_put_drops_the_newline_a_pipe_adds(tmp_path: Path, monkeypatch) -> None:
    import io

    from outcomeci.vault import local as local_vault

    monkeypatch.setenv("OUTCOMECI_CONFIG_HOME", str(tmp_path / "config"))
    assert main(["vault", "local", "init", "--dir", str(tmp_path)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("ghp-token\n"))
    assert main(["vault", "local", "put", "github", "--value-stdin", "--dir", str(tmp_path)]) == 0
    assert local_vault.resolve(tmp_path, "vault:github") == "ghp-token"


def test_agent_only_integration_commands_stay_out_of_help(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit):
        main(["integration", "--help"])
    help_text = capsys.readouterr().out
    assert "{slack}" in help_text
    for hidden in ("list", "describe", "execute", "dry-run", "doctor"):
        assert hidden not in help_text


def test_validate_and_compile_read_the_workflow_in_a_directory(tmp_path: Path, capsys) -> None:
    main(["init", "--dir", str(tmp_path)])
    capsys.readouterr()
    (tmp_path / "outcome.yml").rename(tmp_path / "triage.outcome.yaml")
    config = ["--dir", str(tmp_path), "--config", "triage.outcome.yaml"]
    assert main(["validate", *config]) == 0
    revision = json.loads(capsys.readouterr().out)["workflow_revision"]
    assert main(["workflow", "compile", *config, "--step", "plan"]) == 0
    compiled = json.loads(capsys.readouterr().out)
    assert compiled["workflow_revision"] == revision
    assert compiled["instructions"]["step"]["path"] == ".outcomeci/instructions/plan.md"
    assert main(["workflow", "compile", *config, "--step", "missing"]) == 2


@pytest.mark.parametrize("absolute", [False, True])
def test_integration_reads_config_and_vault_from_workflow_directory(
    tmp_path: Path, capsys, monkeypatch, absolute
) -> None:
    initialize(tmp_path)
    config = tmp_path / "triage.outcome.yaml"
    (tmp_path / "outcome.yml").rename(config)
    roots = []
    monkeypatch.setattr(cli, "local_credential_resolver", lambda root: roots.append(root))
    assert (
        main(
            [
                "integration",
                "list",
                "--dir",
                str(tmp_path),
                "--config",
                str(config) if absolute else config.name,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)
    assert roots == [tmp_path]


def test_cloud_run_flags_are_checked_before_anything_runs(capsys) -> None:
    assert main(["workflow", "run", "--workspace-id", "ws-1"]) == 2
    assert "need --cloud" in capsys.readouterr().err
    assert main(["workflow", "run", "--workflow-id", "wf-1"]) == 2
    assert "need --cloud" in capsys.readouterr().err
    assert main(["workflow", "run", "--cloud", "--workspace-id", "ws-1"]) == 2
    assert "--cloud needs --workspace-id and --workflow-id" in capsys.readouterr().err


def test_workflow_debug_is_gone() -> None:
    import pytest

    with pytest.raises(SystemExit):
        main(["workflow", "debug", "--help"])


def test_every_visible_command_and_option_has_help() -> None:
    import argparse

    from outcomeci.cli import parser

    missing = []

    def walk(current: argparse.ArgumentParser, path: str) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                listed = {choice.dest: choice for choice in action._choices_actions}
                for name, child in action.choices.items():
                    if name in listed:
                        if not listed[name].help:
                            missing.append(f"{path} {name}")
                        walk(child, f"{path} {name}")
            elif not isinstance(action, argparse._HelpAction) and not action.help:
                missing.append(f"{path} {'/'.join(action.option_strings) or action.dest}")

    walk(parser(), "oci")
    assert missing == []
