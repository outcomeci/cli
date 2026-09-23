from __future__ import annotations

import json
import threading
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
