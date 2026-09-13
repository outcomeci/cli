from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

from outcomeci.cli import main
from outcomeci.config import compile_workflow
from outcomeci.repository import initialize, update
from outcomeci.schema import load_schema


def test_init_and_validate(tmp_path: Path, capsys) -> None:
    assert main(["init", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "outcome.yml").is_file()
    assert (tmp_path / ".outcomeci/instructions/standup.md").is_file()
    assert (tmp_path / ".outcomeci/context").is_dir()
    assert (tmp_path / ".agents/skills/outcome/SKILL.md").is_file()
    assert (tmp_path / ".claude/skills/outcome/SKILL.md").is_file()
    assert (tmp_path / ".agents/skills/outcome/SKILL.md").read_text() == (
        tmp_path / ".claude/skills/outcome/SKILL.md"
    ).read_text()
    assert not (tmp_path / ".sp").exists()
    jsonschema.validate(yaml.safe_load((tmp_path / "outcome.yml").read_text()), load_schema())
    assert main(["validate", "--dir", str(tmp_path)]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_schema_can_be_printed_and_exported(tmp_path: Path, capsys) -> None:
    assert main(["schema", "print"]) == 0
    assert json.loads(capsys.readouterr().out)["$id"].endswith("outcome-v1alpha1.schema.json")
    output = tmp_path / "outcome.schema.json"
    assert main(["schema", "export", str(output)]) == 0
    assert json.loads(output.read_text())["title"] == "OutcomeCI Outcome Workflow"


def test_integration_dry_run_and_doctor_are_machine_readable(tmp_path: Path, capsys) -> None:
    initialize(tmp_path, "filesystem")
    assert main(["integration", "dry-run", "--phase", "intake", "--workspace", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["requests_executed"] is False
    assert main(["integration", "doctor", "--workspace", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_outcome_lock_commands(tmp_path: Path, capsys) -> None:
    initialize(tmp_path, "filesystem")
    config, lock = tmp_path / "outcome.yml", tmp_path / "outcome.lock"
    assert main(["outcome", "lock", str(config), "--output", str(lock)]) == 0
    assert json.loads(capsys.readouterr().out)["lock"] == str(lock)
    assert main(["outcome", "verify-lock", str(config), "--lock", str(lock)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_update_refreshes_managed_skills_but_preserves_workflow(tmp_path: Path) -> None:
    initialize(tmp_path, "filesystem")
    workflow = tmp_path / "outcome.yml"
    original = workflow.read_text() + "\n# user policy\n"
    workflow.write_text(original)
    skill = tmp_path / ".agents/skills/outcome/SKILL.md"
    skill.write_text("old managed skill")
    update(tmp_path)
    assert "Only use human tools" in skill.read_text()
    assert workflow.read_text() == original


def test_instruction_content_is_part_of_revision(tmp_path: Path) -> None:
    main(["init", "--dir", str(tmp_path)])
    first = compile_workflow(tmp_path / "outcome.yml")["workflow_revision"]
    instruction = tmp_path / ".outcomeci/instructions/standup.md"
    instruction.write_text(instruction.read_text() + "\nAdditional policy.\n")
    assert compile_workflow(tmp_path / "outcome.yml")["workflow_revision"] != first


def test_legacy_context_is_rejected(tmp_path: Path, capsys) -> None:
    main(["init", "--dir", str(tmp_path)])
    (tmp_path / ".sp").mkdir()
    assert main(["validate", "--dir", str(tmp_path)]) == 2
    assert "legacy .sp context is not supported" in capsys.readouterr().err


def test_filesystem_init_configures_standalone_execution(tmp_path: Path) -> None:
    assert main(["init", "--backend", "filesystem", "--dir", str(tmp_path)]) == 0
    compiled = compile_workflow(tmp_path / "outcome.yml")
    assert compiled["workflow"]["spec"]["backend"]["provider"] == "filesystem"
    assert compiled["workflow"]["spec"]["context"]["provider"] == "filesystem"


def test_filesystem_context_is_hashed_into_revision(tmp_path: Path) -> None:
    main(["init", "--backend", "filesystem", "--dir", str(tmp_path)])
    context = tmp_path / ".outcomeci" / "context"
    evidence = context / "customer-notes.md"
    evidence.write_text("Customers need faster exports.\n")
    first = compile_workflow(tmp_path / "outcome.yml")
    assert first["context"]["files"][0]["path"] == ".outcomeci/context/customer-notes.md"
    assert first["context"]["files"][0]["byte_size"] > 0
    evidence.write_text("Customers need faster and safer exports.\n")
    second = compile_workflow(tmp_path / "outcome.yml")
    assert second["workflow_revision"] != first["workflow_revision"]
    assert second["context"]["files"][0]["sha256"] != first["context"]["files"][0]["sha256"]


def test_filesystem_context_excludes_matching_artifacts(tmp_path: Path) -> None:
    main(["init", "--backend", "filesystem", "--dir", str(tmp_path)])
    (tmp_path / "node_modules" / "package").mkdir(parents=True)
    (tmp_path / "node_modules" / "package" / "notes.md").write_text("ignored\n")
    workflow = tmp_path / "outcome.yml"
    workflow.write_text(workflow.read_text().replace("- .outcomeci/context/**", "- '**/*.md'"))
    paths = [item["path"] for item in compile_workflow(workflow)["context"]["files"]]
    assert "node_modules/package/notes.md" not in paths


def test_local_outcome_status_without_runs(tmp_path: Path, capsys) -> None:
    assert main(["outcome", "status", "--workspace", str(tmp_path)]) == 0
    assert '"status": "no_runs"' in capsys.readouterr().out
