from __future__ import annotations

from pathlib import Path

from outcomeci.cli import main
from outcomeci.config import compile_workflow


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
    assert main(["validate", "--dir", str(tmp_path)]) == 0
    assert '"valid": true' in capsys.readouterr().out


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
