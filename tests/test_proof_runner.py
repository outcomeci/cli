from __future__ import annotations

import json
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
    "init": {"kind": "cmd", "body": "oci init"},
}


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
    workflow = yaml.safe_load((tmp_path / "outcome.yml").read_text())
    assert workflow["apiVersion"] == "outcomeci.workflow/v1"
    assert workflow["trigger"] == "email"


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


def test_vault_credentials_never_exposed_catches_a_leaked_jwt_key(tmp_path: Path) -> None:
    # Every other credential in this proof carries the oci_vault_proof_
    # marker credentials.never_exposed greps for, but a JWT private key is
    # code-generated, not operator-supplied, so it never contains that
    # marker. This proves the assertion still catches a leak of this one
    # credential type instead of being silently exempt from it.
    from outcomeci.proof_runner.credentials import generate_jwt_credential
    from outcomeci.proof_runner.step import _vault_credentials_assertions

    execute("workspace.initialize", tmp_path, {}, False)
    execute("vault.initialize", tmp_path, {}, False)
    context: dict = {}
    generate_jwt_credential(tmp_path, context, {"path": "credentials/jwt", "issuer": "proof"})
    fingerprint = context["generated_secrets"][0]

    # Simulate the realistic leak path: the key ends up nested inside a
    # JSON-serialized ledger event, not printed raw.
    ledger = tmp_path / "leaked-ledger.jsonl"
    ledger.write_text(
        json.dumps({"note": f"leaked key body {fingerprint}"}) + "\n", encoding="utf-8"
    )

    with pytest.raises(ExecutionError, match="failed durability assertions"):
        _vault_credentials_assertions(
            tmp_path,
            context,
            {"ledger": str(ledger), "expected": ["credentials.never_exposed"]},
        )


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
    execute("cli.exec", tmp_path, {"id": "validate", "command": "oci validate"}, False)
    result = execute(
        "simulation.assert",
        tmp_path,
        {
            "expected": [
                "docs.cli_available",
                "docs.init_succeeds",
                "docs.init_creates_workflow",
                "docs.init_creates_step_instructions",
                "docs.workflow_validates",
            ]
        },
        False,
    )

    assert all(item["passed"] for item in result["assertions"])


def test_docs_quickstart_proof_fails_when_a_documented_command_breaks(
    tmp_path: Path, monkeypatch
) -> None:
    broken = dict(QUICKSTART_FIXTURES)
    broken["init"] = {"kind": "cmd", "body": "oci init --template nonexistent"}
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


def test_bundled_webhook_trigger_proof_declares_the_full_contract() -> None:
    definition = bundled_definition("webhook-trigger-v1")
    value = load_definition(definition)
    labels = {
        step["with"]["label"]
        for step in value["spec"]["journey"]
        if step["action"] == "webhook_trigger.fire"
    }
    assert labels == {"accepts_valid_payload", "oversized_payload", "schema_invalid_payload"}
    assert set(value["spec"]["assertions"]) == {
        "webhook_trigger.definition_compiles",
        "webhook_trigger.accepts_valid_payload",
        "webhook_trigger.state_shape_matches_payload",
        "webhook_trigger.intent_falls_back_to_generic_text",
        "webhook_trigger.rejects_oversized_payload",
        "webhook_trigger.rejects_schema_invalid_payload",
    }


def test_webhook_trigger_proof_passes_end_to_end(tmp_path: Path) -> None:
    # Safe to run via run(): the proof stops each run as soon as its state is
    # written, so no step executes and no agent is spawned.
    result = run(bundled_definition("webhook-trigger-v1"), tmp_path)

    assert result["status"] == "passed"
    assert all(item["passed"] for item in result["assertions"])
    assert verify_ledger(Path(result["ledger"]["path"])) == result["ledger"]["sha256"]


def test_webhook_trigger_fire_rejects_an_oversized_payload_for_real(tmp_path: Path) -> None:
    execute("workspace.initialize", tmp_path, {}, False)
    execute("webhook_trigger.configure", tmp_path, {}, False)

    result = execute(
        "webhook_trigger.fire",
        tmp_path,
        {"label": "oversized_payload", "synthetic_oversized": True, "expect_failure": True},
        False,
    )

    assert result == {"status": "rejected", "label": "oversized_payload"}


def test_webhook_trigger_fire_raises_when_an_expected_rejection_does_not_happen(
    tmp_path: Path,
) -> None:
    execute("workspace.initialize", tmp_path, {}, False)
    execute("webhook_trigger.configure", tmp_path, {}, False)

    with pytest.raises(ExecutionError, match="expected accepts_valid_payload to be rejected"):
        execute(
            "webhook_trigger.fire",
            tmp_path,
            {
                "label": "accepts_valid_payload",
                "expect_failure": True,
                "payload": {
                    "schema_version": "outcomeci.trigger.webhook.received/v1",
                    "type": "webhook.received",
                    "event_id": "evt-should-succeed",
                    "received_at": "2026-01-01T00:00:00Z",
                    "method": "POST",
                    "query": "",
                    "headers": {},
                    "body_base64": "",
                },
            },
            False,
        )


def test_webhook_trigger_fire_raises_for_the_wrong_rejection_reason(tmp_path: Path) -> None:
    execute("workspace.initialize", tmp_path, {}, False)
    execute("webhook_trigger.configure", tmp_path, {}, False)

    with pytest.raises(ExecutionError, match="rejected for the wrong reason"):
        execute(
            "webhook_trigger.fire",
            tmp_path,
            {
                "label": "oversized_payload",
                "synthetic_oversized": True,
                "expect_failure": True,
                "expect_error_contains": "this string never appears in the real error",
            },
            False,
        )


def test_webhook_trigger_fire_propagates_an_unexpected_rejection(tmp_path: Path) -> None:
    execute("workspace.initialize", tmp_path, {}, False)
    execute("webhook_trigger.configure", tmp_path, {}, False)

    with pytest.raises(ExecutionError):
        execute(
            "webhook_trigger.fire",
            tmp_path,
            {
                "label": "schema_invalid_payload",
                "payload": {
                    "schema_version": "outcomeci.trigger.webhook.received/v1",
                    "type": "webhook.received",
                    "received_at": "2026-01-01T00:00:00Z",
                    "method": "POST",
                    "query": "",
                    "headers": {},
                    "body_base64": "",
                },
            },
            False,
        )
