from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from outcomeci.process import ExecutionError
from outcomeci.simulation import bundled_definition, load_definition, run, verify_ledger
from outcomeci.simulation_step import execute


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
