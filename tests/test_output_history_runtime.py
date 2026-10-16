import json

import pytest
import yaml

from outcomeci.runtime import output_history
from outcomeci.runtime.engine import ExecutionOptions
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import ConfigError, compile_workflow


def workflow(tmp_path, *, bad=None):
    document = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "outputs": {
            "sources": {
                "past": {"workflow": "self", "steps": ["assess"], "fields": ["customer", "summary"]}
            },
            "queries": {"previous": {"source": "past", "where": {"customer": {"eq": "alice"}}}},
        },
        "steps": [
            {
                "assess": {
                    "reason": "Assess",
                    "with": ["history.previous"],
                    "returns": {"customer": "string", "summary": "string"},
                    "index": {"fields": ["customer"]},
                }
            }
        ],
    }
    if bad:
        bad(document)
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(document))
    return path, compile_workflow(path)


def state(run):
    return {"run_id": run, "step": "assess", "status": "running"}


def test_local_history_pins_and_projects_without_implicit_inputs(tmp_path):
    config, compiled = workflow(tmp_path)
    options = ExecutionOptions()
    first = state("first")
    output_history.prepare(tmp_path, config, compiled, first, options)
    assert output_history.inputs(first) == {"previous": []}
    artifact = tmp_path / ".outcomeci/outcomes/first/assess/outputs.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"customer": "alice", "summary": "approved"}))
    first["status"] = "completed"
    output_history.publish(tmp_path, compiled, first, "assess")
    assert first["output_history"]["publication_status"] == "ready"
    second = state("second")
    output_history.prepare(tmp_path, config, compiled, second, options)
    assert output_history.inputs(second) == {
        "previous": [{"customer": "alice", "summary": "approved"}]
    }
    assert output_history.inputs(first) == {"previous": []}
    assert artifact.read_text() == json.dumps({"customer": "alice", "summary": "approved"})


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["outputs"]["queries"]["previous"].update(limit=101),
        lambda d: d["outputs"]["queries"]["previous"].update(where={"summary": {"eq": "x"}}),
        lambda d: d["outputs"]["queries"]["previous"].update(
            where={"customer": {"eq": {"ref": "assess.customer"}}}
        ),
        lambda d: d["steps"][0]["assess"]["index"].update(fields=["missing"]),
    ],
)
def test_compiler_rejects_query_grant_expansion(tmp_path, change):
    with pytest.raises(ConfigError):
        workflow(tmp_path, bad=change)


def test_missing_pinned_snapshot_fails_without_refresh(tmp_path):
    config, compiled = workflow(tmp_path)
    run = state("run")
    output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())
    (tmp_path / run["output_history"]["snapshot_path"]).unlink()
    run["output_history"]["receipts"].clear()
    with pytest.raises(ExecutionError, match="snapshot"):
        output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())


@pytest.mark.parametrize("placement", ["workflow", "step"])
def test_engine_queries_history_into_explicit_step_context(tmp_path, monkeypatch, placement):
    from contextlib import nullcontext

    from outcomeci.runtime import engine

    def placement_config(document):
        if placement == "step":
            document["steps"][0]["assess"]["query"] = document["outputs"].pop("queries")

    config, _ = workflow(tmp_path, bad=placement_config)
    contexts = []
    monkeypatch.setattr(engine, "serve_capability", lambda *a, **k: nullcontext({}))
    monkeypatch.setattr(
        engine, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    original = engine._step_context

    def capture(*args, **kwargs):
        context = original(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(engine, "_step_context", capture)

    def agent(*args, **kwargs):
        context = contexts[-1]
        from pathlib import Path

        Path(context["returns"]["path"]).write_text(
            json.dumps({"customer": "alice", "summary": "approved"})
        )
        return "done"

    monkeypatch.setattr(engine, "invoke", agent)
    options = ExecutionOptions(credential_resolver=lambda ref: "unused")
    first = engine.trigger(tmp_path, config, "manual", {"subject": "first"}, options=options)
    second = engine.trigger(tmp_path, config, "manual", {"subject": "second"}, options=options)
    assert first["status"] == second["status"] == "completed"
    assert contexts[0]["inputs"][0]["value"] == []
    assert contexts[-1]["inputs"][0]["value"] == [{"customer": "alice", "summary": "approved"}]
    persisted = engine._read(tmp_path, second["run_id"])
    assert persisted["output_history"]["publication_status"] == "ready"


def test_container_history_import_merges_without_replacing_database(tmp_path):
    import shutil

    from outcomeci.runtime.engine import _write
    from outcomeci.runtime.launcher import _import_run_state

    host = tmp_path / "host"
    host.mkdir()
    config, compiled = workflow(host)
    workflow_id = output_history.identity(host, config)
    work = tmp_path / "work"
    shutil.copytree(host, work)
    run = state("20261016-first")
    output_history.prepare(work, work / config.name, compiled, run, ExecutionOptions())
    artifact = work / ".outcomeci/outcomes" / run["run_id"] / "assess/outputs.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"customer": "alice", "summary": "approved"}))
    run["status"] = "completed"
    output_history.publish(work, compiled, run, "assess")
    _write(work, run)
    _import_run_state(work, host)
    output_history.import_history(work, host, config)
    assert output_history.identity(host, config) == workflow_id
    later = state("later")
    output_history.prepare(host, config, compiled, later, ExecutionOptions())
    assert output_history.inputs(later)["previous"][0]["summary"] == "approved"


def test_managed_history_uses_claim_pin_and_revalidates_cached_receipts(tmp_path):
    config, compiled = workflow(tmp_path)
    calls = []

    def client(action, payload):
        calls.append((action, payload))
        if action == "pin":
            return {"generation": 4, "digest": "server-digest"}
        return {"records": [], "snapshot": {"generation": 4}, "next_cursor": None}

    options = ExecutionOptions(output_history_client=client)
    run = state("managed")
    output_history.prepare(tmp_path, config, compiled, run, options)
    output_history.prepare(tmp_path, config, compiled, run, options)
    assert [action for action, _ in calls] == ["pin", "query", "query"]
    assert calls[1][1]["source"] == "past"
    assert calls[1][1]["query_name"] == "previous"
    assert not (tmp_path / ".outcomeci/output-history").exists()


def test_required_empty_is_durably_failed_before_agent(tmp_path, monkeypatch):
    from outcomeci.runtime import engine

    config, _ = workflow(
        tmp_path, bad=lambda d: d["outputs"]["queries"]["previous"].update(required=True)
    )
    monkeypatch.setattr(engine, "invoke", lambda *a, **k: pytest.fail("agent must not execute"))
    with pytest.raises(ExecutionError):
        engine.trigger(tmp_path, config, "manual", {"subject": "empty"}, options=ExecutionOptions())
    saved = json.loads(next((tmp_path / ".outcomeci/outcomes").glob("*/run.json")).read_text())
    assert saved["status"] == "error"
    assert saved["completed_steps"] == []


def test_sidecar_failure_recovers_original_accepted_evidence(tmp_path, monkeypatch):
    config, compiled = workflow(tmp_path)
    run = state("recovery")
    output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())
    artifact = tmp_path / ".outcomeci/outcomes/recovery/assess/outputs.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"customer": "alice", "summary": "ok"}))
    output_history.accept(tmp_path, compiled, run, "assess")
    accepted = json.loads(json.dumps(run["output_history"]["accepted_records"]))
    run.update(status="completed", completed_steps=["assess"])
    original = output_history.atomic_write_json
    monkeypatch.setattr(
        output_history,
        "atomic_write_json",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    output_history.publish(tmp_path, compiled, run, "assess")
    assert run["output_history"]["publication_status"] == "failed"
    monkeypatch.setattr(output_history, "atomic_write_json", original)
    output_history.publish(tmp_path, compiled, run)
    assert run["output_history"]["publication_status"] == "ready"
    assert run["output_history"]["accepted_records"] == accepted
    artifact.write_text('{"customer":"alice","summary":"modified"}')
    output_history.publish(tmp_path, compiled, run)
    assert run["output_history"]["publication_status"] == "failed"


def test_history_read_rejects_symlink_snapshot(tmp_path):
    config, compiled = workflow(tmp_path)
    run = state("unsafe")
    output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())
    snapshot = tmp_path / run["output_history"]["snapshot_path"]
    original = snapshot.with_suffix(".other")
    snapshot.rename(original)
    snapshot.symlink_to(original)
    with pytest.raises(ExecutionError, match="symlink"):
        output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())


def test_copy_history_includes_committed_wal_rows(tmp_path):
    import sqlite3

    source = tmp_path / "source"
    target = tmp_path / "target"
    path = source / ".outcomeci/output-history/example/outputs.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE evidence (value TEXT)")
        writer.execute("INSERT INTO evidence VALUES ('committed')")
        writer.commit()
        output_history.copy_history(source, target)
        with sqlite3.connect(target / path.relative_to(source)) as copied:
            assert copied.execute("SELECT value FROM evidence").fetchone()[0] == "committed"


def test_loop_acceptance_ignores_stale_item_files(tmp_path):
    config, compiled = workflow(tmp_path)
    run = state("loop")
    output_history.prepare(tmp_path, config, compiled, run, ExecutionOptions())
    block = compiled["instructions"]["steps"]["assess"]["v1"]
    block["for_each"] = {"ref": "trigger.items", "as": "item"}
    block["returns"]["item"] = {"path": "assess/items/{index}.json"}
    directory = tmp_path / ".outcomeci/outcomes/loop/assess/items"
    directory.mkdir(parents=True)
    for index in (0, 1, 99):
        (directory / f"{index}.json").write_text(
            json.dumps({"customer": "alice", "summary": str(index)})
        )
    output_history.accept(tmp_path, compiled, run, "assess", count=2)
    assert len(run["output_history"]["accepted_records"]) == 2
    assert all("99.json" not in r["artifact_id"] for r in run["output_history"]["accepted_records"])


def test_history_sandbox_masks_other_runs_and_receipts(tmp_path, monkeypatch):
    from outcomeci.runtime import process

    current = tmp_path / ".outcomeci/outcomes/current"
    old = tmp_path / ".outcomeci/outcomes/old"
    old.mkdir(parents=True)
    (current / "step").mkdir(parents=True)
    output = current / "step/outputs.json"
    output.touch()
    (current / "run.json").write_text('{"private":"other query data"}')
    (current / "previous-step").mkdir()
    calls = []
    monkeypatch.setattr(process.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        process,
        "command",
        lambda argv, **kwargs: calls.append(argv) or process.Result(0, "done", ""),
    )
    process.invoke(
        "codex",
        None,
        "prompt",
        tmp_path,
        10,
        allow_local_auth=True,
        writable_paths=[output],
        container_isolated=False,
        extra_env={"OUTCOMECI_OUTPUT_HISTORY": "1"},
    )
    argv = calls[0]
    assert argv[0] == "/usr/bin/bwrap"
    assert ["--tmpfs", str(old)] in [argv[i : i + 2] for i in range(len(argv))]
    assert ["--tmpfs", str(current / "previous-step")] in [
        argv[i : i + 2] for i in range(len(argv))
    ]
    assert ["--ro-bind", "/dev/null", str(current / "run.json")] in [
        argv[i : i + 3] for i in range(len(argv))
    ]
