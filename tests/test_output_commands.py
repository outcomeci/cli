import json

import pytest
from test_output_history_runtime import state, workflow

from outcomeci.artifacts import output_commands
from outcomeci.cli import main
from outcomeci.runtime import output_history
from outcomeci.runtime.engine import ExecutionOptions
from outcomeci.runtime.process import ExecutionError


def produced(tmp_path):
    config, compiled = workflow(tmp_path)
    run = state("first")
    output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())
    folder = tmp_path / ".outcomeci/outcomes/first"
    artifact = folder / "assess/outputs.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"customer": "alice", "summary": "approved"}))
    run["status"] = "completed"
    output_history.publish(tmp_path, compiled, run, "assess")
    (folder / "run.json").write_text(json.dumps(run))
    return config, run, artifact


def test_cli_queries_declared_projection_and_rejects_undeclared_source(tmp_path, capsys):
    config, _, _ = produced(tmp_path)
    args = ["outputs", "query", "--dir", str(tmp_path), "--config", str(config)]
    assert (
        main([*args, "--source", "past", "--query", '{"where":{"customer":{"eq":"alice"}}}']) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["records"][0]["values"] == {"customer": "alice", "summary": "approved"}
    assert main([*args, "--source", "unknown"]) == 2
    assert "declared" in capsys.readouterr().err
    assert main([*args, "--source", "past", "--query", "[]"]) == 2
    assert "JSON object" in capsys.readouterr().err


def test_rebuild_recovers_deleted_index_without_touching_json(tmp_path):
    config, run, artifact = produced(tmp_path)
    original = artifact.read_bytes()
    db = output_history.database(tmp_path, run["output_history"]["workflow_id"])
    db.unlink()
    rebuilt = output_commands.rebuild(tmp_path, config)
    assert rebuilt["indexed"] == 1 and rebuilt["complete"]
    assert output_commands.rebuild(tmp_path, config)["indexed"] == 0
    assert output_commands.query(tmp_path, config, "past", {})["records"]
    assert artifact.read_bytes() == original


def test_rebuild_recovers_missing_sidecar_from_accepted_run_evidence(tmp_path):
    config, run, artifact = produced(tmp_path)
    output_history.database(tmp_path, run["output_history"]["workflow_id"]).unlink()
    (artifact.parent.parent / "output-index.json").unlink()
    result = output_commands.rebuild(tmp_path, config)
    assert result["complete"] and result["indexed"] == 1
    assert output_commands.query(tmp_path, config, "past", {})["records"]


def test_deleted_artifact_is_not_readable_or_resurrected(tmp_path):
    config, _, artifact = produced(tmp_path)
    artifact.unlink()
    with pytest.raises(ExecutionError, match="deleted"):
        output_commands.query(tmp_path, config, "past", {})
    rebuilt = output_commands.rebuild(tmp_path, config)
    assert not rebuilt["complete"]
    assert output_commands.query(tmp_path, config, "past", {})["records"] == []


def test_rebuild_rejects_cross_run_artifact_reference(tmp_path):
    config, _, _ = produced(tmp_path)
    sidecar = tmp_path / ".outcomeci/outcomes/first/output-index.json"
    evidence = json.loads(sidecar.read_text())
    evidence["records"][0]["artifact_id"] = ".outcomeci/outcomes/other/output.json"
    sidecar.write_text(json.dumps(evidence))
    with pytest.raises(ExecutionError, match="different run"):
        output_commands.rebuild(tmp_path, config)


def test_query_rejects_symlinked_source_even_with_matching_bytes(tmp_path):
    config, _, artifact = produced(tmp_path)
    elsewhere = tmp_path / "outside.json"
    elsewhere.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(elsewhere)
    with pytest.raises(ExecutionError, match="symlink"):
        output_commands.query(tmp_path, config, "past", {})


def test_rebuild_reports_malformed_record_without_traceback(tmp_path):
    config, _, _ = produced(tmp_path)
    sidecar = tmp_path / ".outcomeci/outcomes/first/output-index.json"
    sidecar.write_text(json.dumps({"schema_version": 1, "records": ["invalid"]}))
    with pytest.raises(ExecutionError, match="record must be an object"):
        output_commands.rebuild(tmp_path, config)
