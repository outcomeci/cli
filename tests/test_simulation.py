from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from outcomeci.process import ExecutionError
from outcomeci.simulation import bundled_definition, load_definition, run, verify_ledger
from outcomeci.simulation_step import execute

QUICKSTART_FIXTURES = {
    "cli-available": {"kind": "cmd", "body": "oci --help"},
    "init": {"kind": "cmd", "body": "oci init --backend filesystem"},
    "outcomes-dir": {"kind": "path", "body": ".outcomeci/outcomes/<run-id>/"},
    "status": {"kind": "cmd", "body": "oci outcome status <run-id>"},
}


def test_bundled_local_first_proof_survives_faults(tmp_path: Path) -> None:
    result = run(None, tmp_path)

    assert result["status"] == "passed"
    assert result["recoveries"] == 2
    assert result["faults_injected"] == ["complete_intake", "complete_plan"]
    assert result["final_state"] == {
        "status": "ready_for_implementation",
        "phase": "tasks",
        "completed_phases": ["intake", "plan", "tasks"],
    }
    assert all(item["passed"] for item in result["assertions"])
    assert verify_ledger(Path(result["ledger"]["path"])) == result["ledger"]["sha256"]


def test_proof_evidence_never_contains_the_vault_canary(tmp_path: Path) -> None:
    result = run(None, tmp_path)
    root = Path(result["ledger"]["path"]).parents[1]
    context = json.loads((root / "workspace/.outcomeci/simulation-context.json").read_text())
    evidence = Path(result["ledger"]["path"]).read_text() + json.dumps(result)
    vault = (root / "workspace/.outcomeci/vault.enc").read_text()

    assert context["canary_sha256"] not in evidence
    assert "oci_sim_" not in evidence
    assert "oci_sim_" not in vault


def test_proof_definition_rejects_unknown_actions(tmp_path: Path) -> None:
    value = yaml.safe_load(bundled_definition().read_text())
    value["spec"]["journey"][0]["action"] = "shell.execute"
    path = tmp_path / "proof.yml"
    path.write_text(yaml.safe_dump(value))

    with pytest.raises(ExecutionError, match="unsupported simulation action"):
        load_definition(path)


def test_proof_definition_requires_outcome_proof_kind(tmp_path: Path) -> None:
    value = yaml.safe_load(bundled_definition().read_text())
    value["kind"] = "OutcomeWorkflow"
    path = tmp_path / "proof.yml"
    path.write_text(yaml.safe_dump(value))

    with pytest.raises(ExecutionError, match="unsupported proof definition"):
        load_definition(path)


def test_bundled_email_trigger_proof_declares_real_cloud_journey() -> None:
    definition = bundled_definition("email-trigger-v1")
    value = load_definition(definition)
    assert value["spec"]["persona"]["starting_state"]["network"] == "managed_cloud"
    assert [step["action"] for step in value["spec"]["journey"]][-3:] == [
        "email.send",
        "email.wait",
        "console.log",
    ]


def test_email_proof_actions_use_workspace_api_and_emit_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("OUTCOMECI_PROOF_API_URL", "https://api.example.test")
    monkeypatch.setenv("OUTCOMECI_PROOF_API_KEY", "oci_" + "a" * 32)
    monkeypatch.setenv("OUTCOMECI_PROOF_WORKSPACE_ID", "workspace_test")
    monkeypatch.setattr("outcomeci.simulation_step.login_with_key", lambda *_args: {})
    monkeypatch.setattr(
        "outcomeci.simulation_step.sync_workflow", lambda *_args, **_kwargs: {"revision": 1}
    )
    monkeypatch.setattr(
        "outcomeci.simulation_step.start_email_trigger_proof",
        lambda _workspace: {"proof_id": "00000000-0000-4000-8000-000000000001", "status": "sent"},
    )
    monkeypatch.setattr(
        "outcomeci.simulation_step.get_email_trigger_proof",
        lambda *_args: {
            "proof_id": "00000000-0000-4000-8000-000000000001",
            "status": "completed",
            "ingress_status": "processed",
            "invocation_status": "completed",
            "invocation_count": 1,
            "artifact_count": 2,
            "attachment_count": 1,
            "usage": [],
            "events": [
                {"event_type": "trigger.received", "payload": {}},
                {"event_type": "console.logged", "payload": {"attachment_count": 1}},
            ],
        },
    )

    execute("workspace.initialize", tmp_path, {}, False)
    execute("cloud.authenticate", tmp_path, {}, False)
    execute("workflow.configure_email", tmp_path, {}, False)
    execute("workflow.sync", tmp_path, {}, False)
    execute("email.send", tmp_path, {}, False)
    execute("email.wait", tmp_path, {"timeout_seconds": 1}, False)
    result = execute("console.log", tmp_path, {}, False)

    assert result == {"status": "logged", "message": "email received"}
    assert "email received proof=00000000-0000-4000-8000-000000000001" in capsys.readouterr().out


def test_bundled_docs_quickstart_proof_declares_real_cli_journey() -> None:
    definition = bundled_definition("docs-quickstart-v1")
    value = load_definition(definition)
    assert [step["action"] for step in value["spec"]["journey"]] == [
        "docs.fetch",
        "cli.exec",
        "cli.exec",
        "cli.exec",
        "cli.exec",
    ]
    assert value["spec"]["assertions"][0] == "docs.cli_available"


def test_docs_fetch_extracts_marked_fixtures_over_http(tmp_path: Path, monkeypatch) -> None:
    markdown = '# Quickstart\n\n<!-- proof:cmd id="cli-available" -->\n```sh\noci --help\n```\n'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = markdown.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "OUTCOMECI_DOCS_BASE_URL", f"http://127.0.0.1:{server.server_port}/quickstart-base"
    )
    try:
        result = execute("docs.fetch", tmp_path, {"page": "quickstart"}, False)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result == {"status": "fetched", "page": "quickstart", "fixtures": ["cli-available"]}


def test_docs_quickstart_proof_runs_the_real_cli_against_fetched_fixtures(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "outcomeci.simulation_step.fetch_fixtures", lambda page: QUICKSTART_FIXTURES
    )

    execute("docs.fetch", tmp_path, {"page": "quickstart"}, False)
    execute("cli.exec", tmp_path, {"page": "quickstart", "fixture": "cli-available"}, False)
    execute("cli.exec", tmp_path, {"page": "quickstart", "fixture": "init"}, False)
    execute(
        "cli.exec",
        tmp_path,
        {
            "id": "bootstrap-run",
            "command": 'oci outcome begin "Improve the first-run experience for new users"',
            "capture": {"run_id": "run_id"},
        },
        False,
    )
    execute(
        "cli.exec",
        tmp_path,
        {"page": "quickstart", "fixture": "status", "substitute": {"<run-id>": "run_id"}},
        False,
    )
    result = execute(
        "simulation.assert",
        tmp_path,
        {
            "expected": [
                "docs.cli_available",
                "docs.init_succeeds",
                "docs.init_creates_workflow",
                "docs.init_creates_agent_instructions",
                "docs.status_succeeds",
                "docs.status_reports_run_id",
                "docs.run_artifacts_recorded",
            ]
        },
        False,
    )

    assert all(item["passed"] for item in result["assertions"])


def test_docs_quickstart_proof_fails_when_a_documented_command_breaks(
    tmp_path: Path, monkeypatch
) -> None:
    broken = dict(QUICKSTART_FIXTURES)
    broken["init"] = {"kind": "cmd", "body": "oci init --backend nonexistent-backend"}
    monkeypatch.setattr("outcomeci.simulation_step.fetch_fixtures", lambda page: broken)

    execute("docs.fetch", tmp_path, {"page": "quickstart"}, False)
    with pytest.raises(ExecutionError, match="documented command failed"):
        execute("cli.exec", tmp_path, {"page": "quickstart", "fixture": "init"}, False)


def test_cli_exec_refuses_to_run_a_non_oci_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "outcomeci.simulation_step.fetch_fixtures",
        lambda page: {"escape": {"kind": "cmd", "body": "rm -rf /"}},
    )

    execute("docs.fetch", tmp_path, {"page": "quickstart"}, False)
    with pytest.raises(ExecutionError, match="only runs documented `oci` commands"):
        execute("cli.exec", tmp_path, {"page": "quickstart", "fixture": "escape"}, False)
