from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from outcomeci.process import ExecutionError
from outcomeci.proof_runner.simulation import (
    bundled_definition,
    load_definition,
    run,
    verify_ledger,
)
from outcomeci.proof_runner.step import execute

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
    monkeypatch.setattr("outcomeci.proof_runner.step.login_with_key", lambda *_args: {})
    monkeypatch.setattr(
        "outcomeci.proof_runner.step.sync_workflow", lambda *_args, **_kwargs: {"revision": 1}
    )
    monkeypatch.setattr(
        "outcomeci.proof_runner.step.start_email_trigger_proof",
        lambda _workspace: {"proof_id": "00000000-0000-4000-8000-000000000001", "status": "sent"},
    )
    monkeypatch.setattr(
        "outcomeci.proof_runner.step.get_email_trigger_proof",
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


def test_bundled_vault_credentials_proof_declares_every_auth_type() -> None:
    definition = bundled_definition("vault-credentials-v1")
    value = load_definition(definition)
    auth_types = {
        step["with"]["auth_type"]
        for step in value["spec"]["journey"]
        if step["action"] == "connection.authenticate"
    }
    assert auth_types == {
        "api_key",
        "basic",
        "bearer",
        "oauth2_client_credentials",
        "oauth2_refresh_token",
        "jwt_bearer",
    }
    assert "credentials.never_exposed" in value["spec"]["assertions"]


def test_vault_credentials_proof_passes_end_to_end(tmp_path: Path) -> None:
    result = run(bundled_definition("vault-credentials-v1"), tmp_path)

    assert result["status"] == "passed"
    assert all(item["passed"] for item in result["assertions"])
    assert verify_ledger(Path(result["ledger"]["path"])) == result["ledger"]["sha256"]


def test_mock_authorization_server_rejects_the_wrong_bearer_token(tmp_path: Path) -> None:
    # Every credential.*_authenticates assertion is only meaningful if the
    # mock authorization server it runs against can actually say no. This
    # proves it does, independent of the vault or the rotation bookkeeping.
    from outcomeci.proof_runner.credentials import _mock_authorization_server

    server = _mock_authorization_server("bearer", {"value": "correct-token"})
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wrong = httpx.get(
            f"http://127.0.0.1:{port}/verify", headers={"Authorization": "Bearer wrong-token"}
        )
        right = httpx.get(
            f"http://127.0.0.1:{port}/verify", headers={"Authorization": "Bearer correct-token"}
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert wrong.status_code == 401
    assert wrong.json() == {"accepted": False}
    assert right.status_code == 200
    assert right.json() == {"accepted": True}


def test_vault_rotate_credential_requires_a_prior_put(tmp_path: Path) -> None:
    execute("workspace.initialize", tmp_path, {}, False)
    execute("vault.initialize", tmp_path, {}, False)

    with pytest.raises(ExecutionError, match="no credential was previously stored"):
        execute(
            "vault.rotate_credential",
            tmp_path,
            {"path": "credentials/never-stored", "value": {"value": "x"}},
            False,
        )


def test_cloud_vault_rotate_requires_a_prior_put(tmp_path: Path) -> None:
    execute("workspace.initialize", tmp_path, {}, False)
    execute("cloud.mock_session", tmp_path, {}, False)

    with pytest.raises(ExecutionError, match="no cloud Vault entry was stored"):
        execute(
            "cloud.vault_rotate",
            tmp_path,
            {"path": "cloud/never-stored", "value": "x"},
            False,
        )


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
        "outcomeci.proof_runner.step.fetch_fixtures", lambda page: QUICKSTART_FIXTURES
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
    monkeypatch.setattr("outcomeci.proof_runner.step.fetch_fixtures", lambda page: broken)

    execute("docs.fetch", tmp_path, {"page": "quickstart"}, False)
    with pytest.raises(ExecutionError, match="documented command failed"):
        execute("cli.exec", tmp_path, {"page": "quickstart", "fixture": "init"}, False)


def test_cli_exec_refuses_to_run_a_non_oci_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "outcomeci.proof_runner.step.fetch_fixtures",
        lambda page: {"escape": {"kind": "cmd", "body": "rm -rf /"}},
    )

    execute("docs.fetch", tmp_path, {"page": "quickstart"}, False)
    with pytest.raises(ExecutionError, match="only runs documented `oci` commands"):
        execute("cli.exec", tmp_path, {"page": "quickstart", "fixture": "escape"}, False)


def _fake_intake_agent(valid: bool = True):
    """A stand-in for a real Claude/Codex session: writes the intake
    artifact the real prompt asks for, parsing the ontology revision id out
    of the prompt exactly like a real agent would read it, not hardcoding
    it. Not a substitute for agent-driven-v1 actually running against a real
    model with live credentials -- that's the whole point of this proof, and
    it isn't something this test suite can do safely or for free.
    """

    def fake_invoke(agent, model, prompt, workspace, timeout, **kwargs):
        match = re.search(r"beneath (.+?)\. During", prompt)
        assert match
        root = Path(match.group(1))
        root.mkdir(parents=True, exist_ok=True)
        revision = re.search(r'ontology_revision_id\s+\\?"([^"\\]+)', prompt)
        assert revision
        target = root / "intake" / "trajectory.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if valid:
            targets = [
                {
                    "repository_id": f"local:{workspace.name}",
                    "repository": workspace.name,
                    "rationale": "top-level layout",
                    "candidates": [],
                }
            ]
        else:
            targets = []  # _execute's own _validate_trajectory rejects this immediately
        target.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "ontology_revision_id": revision.group(1),
                    "targets": targets,
                }
            )
        )
        return "intake complete"

    return fake_invoke


def test_bundled_agent_driven_proof_declares_both_agents() -> None:
    definition = bundled_definition("agent-driven-v1")
    value = load_definition(definition)
    agents = {
        step["with"]["agent"]
        for step in value["spec"]["journey"]
        if step["action"] == "agent.start_run"
    }
    assert agents == {"claude", "codex"}
    assert set(value["spec"]["assertions"]) == {
        "agent.claude_completes_intake",
        "agent.claude_writes_valid_artifacts",
        "agent.codex_completes_intake",
        "agent.codex_writes_valid_artifacts",
    }


def test_agent_driven_proof_passes_with_a_stand_in_agent(tmp_path: Path, monkeypatch) -> None:
    # Proves the proof's own wiring -- start -> local approval -> verify ->
    # assert, for two independent runs sharing one workspace -- is correct.
    # Does not and cannot prove a real model behaves correctly; that needs
    # real credentials, which is why this proof is not run in CI. Uses
    # execute() directly rather than run(): run() spawns each journey step
    # in an isolated subprocess, which an in-process monkeypatch of
    # local.invoke can never reach -- that would fall through to actually
    # invoking a real claude/codex binary.
    monkeypatch.setattr("outcomeci.local.invoke", _fake_intake_agent(valid=True))
    intent = "List every top-level directory in this repository."

    execute("workspace.initialize", tmp_path, {}, False)
    for agent in ("claude", "codex"):
        execute("agent.start_run", tmp_path, {"agent": agent, "intent": intent}, False)
        execute("agent.approve_intake", tmp_path, {"agent": agent}, False)
        execute("agent.verify_run", tmp_path, {"agent": agent}, False)
    result = execute(
        "simulation.assert",
        tmp_path,
        {
            "expected": [
                "agent.claude_completes_intake",
                "agent.claude_writes_valid_artifacts",
                "agent.codex_completes_intake",
                "agent.codex_writes_valid_artifacts",
            ],
            "ledger": str(tmp_path / "ledger.jsonl"),
        },
        False,
    )

    assert all(item["passed"] for item in result["assertions"])


def test_agent_start_run_rejects_an_invalid_intake_artifact(tmp_path: Path, monkeypatch) -> None:
    # This assertion is a real check, not vacuous: an agent that writes a
    # malformed intake trajectory (here, no repository targets) never gets
    # to complete the run at all -- _execute's own _validate_trajectory
    # rejects it before agent.verify_run would ever see it.
    monkeypatch.setattr("outcomeci.local.invoke", _fake_intake_agent(valid=False))

    execute("workspace.initialize", tmp_path, {}, False)
    with pytest.raises(ExecutionError, match="agent returned no intake repository targets"):
        execute(
            "agent.start_run",
            tmp_path,
            {"agent": "claude", "intent": "List every top-level directory."},
            False,
        )


def test_agent_approve_intake_requires_a_started_run(tmp_path: Path) -> None:
    execute("workspace.initialize", tmp_path, {}, False)

    with pytest.raises(ExecutionError, match="no run was started for agent claude"):
        execute("agent.approve_intake", tmp_path, {"agent": "claude"}, False)
