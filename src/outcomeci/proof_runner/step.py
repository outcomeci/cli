"""Isolated process actions used by the ecosystem simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..cloud import (
    get_email_trigger_proof,
    login_with_key,
    start_email_trigger_proof,
    sync_workflow,
)
from ..config import compile_workflow
from ..integrations import IntegrationExecutor, local_credential_resolver
from ..local import advance, begin, compile_context, respond, validate_artifacts
from ..local_vault import initialize as initialize_vault
from ..local_vault import put as put_vault
from ..local_vault import resolve as resolve_vault
from ..process import ExecutionError
from ..repository import initialize, validate
from . import cloud_vault, credentials
from .simulation import FAULT_EXIT

CONTEXT = Path(".outcomeci/simulation-context.json")


def _read(root: Path) -> dict[str, Any]:
    path = root / CONTEXT
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(root: Path, value: dict[str, Any]) -> None:
    path = root / CONTEXT
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _configure(root: Path) -> None:
    path = root / "outcome.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = workflow["spec"]
    spec["connections"] = [
        {
            "ref": "simulation_local",
            "provider": "http",
            "base_url": "http://127.0.0.1:8765",
            "allow_private_network": True,
            "auth": {"type": "bearer", "credential": "vault:simulation/api_token"},
        }
    ]
    spec["integrations"] = {
        "simulation": {
            "connection": "simulation_local",
            "access": {"mode": "schema"},
            "operations": {
                "verify": {
                    "description": "Verify credential-blind local execution.",
                    "policy": {
                        "side_effect": "read",
                        "approval": "none",
                        "idempotency": "supported",
                    },
                    "input": {"type": "object", "additionalProperties": False},
                    "request": {"method": "GET", "path": "/verify"},
                    "response": {"expose": {"accepted": "body.accepted"}},
                }
            },
        }
    }
    intake = spec["agents"]["phases"]["intake"]
    intake["integrations"].insert(0, {"type": "api", "capability": "simulation.verify"})
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")


def _configure_email(root: Path) -> None:
    path = root / "outcome.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    proof_name = root.parent.name.replace("_", "-")
    workflow["metadata"]["name"] = f"email-{proof_name}"[:100]
    workflow["spec"]["backend"] = {"provider": "outcomeci"}
    workflow["spec"]["context"] = {"provider": "outcomeci"}
    workflow["spec"]["triggers"] = {
        "inbound_email": {
            "type": "email.received",
            "filters": {"subject_prefix": "OutcomeCI email trigger proof"},
        }
    }
    intake = workflow["spec"]["agents"]["phases"]["intake"]
    intake["expects"]["inputs"] = [
        {
            "name": "email",
            "from": "trigger.inbound_email",
            "media_type": "application/json",
        }
    ]
    path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")


def _integration(root: Path) -> dict[str, Any]:
    expected = resolve_vault(root, "vault:simulation/api_token")
    observed = {"authorized": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            observed["authorized"] = self.headers.get("Authorization") == f"Bearer {expected}"
            body = json.dumps({"accepted": observed["authorized"]}).encode()
            self.send_response(200 if observed["authorized"] else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        result = IntegrationExecutor(
            compile_workflow(root / "outcome.yml"),
            resolver=local_credential_resolver(root),
            transport=httpx.HTTPTransport(),
        ).execute("simulation.verify", {}, phase="intake")
    finally:
        server.server_close()
        thread.join(timeout=2)
    if result.get("output") != {"accepted": True} or not observed["authorized"]:
        raise ExecutionError("local Vault credential was not applied by the capability broker")
    return {"status": "verified", "credential_exposed": False}


def _materialize(root: Path, phase: str) -> dict[str, Any]:
    state = _read(root)
    run_id = state["run_id"]
    context = compile_context(root, root / "outcome.yml", run_id)
    if context["run"]["phase"] != phase:
        raise ExecutionError(f"expected phase {phase}, found {context['run']['phase']}")
    outcome_root = Path(context["outcome_root"])
    (outcome_root / "standup.md").write_text(
        "# Standup: simulation\n**Status**: active\n\nDeterministic persona evidence.\n",
        encoding="utf-8",
    )
    for contract in context["instructions"]["phase"]["expects"]["outputs"]:
        path = outcome_root / contract["path"]
        if contract["media_type"] == "inode/directory":
            path.mkdir(parents=True, exist_ok=True)
            (path / "simulation.md").write_text(
                "# Deterministic simulation artifact\n", encoding="utf-8"
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if phase == "intake" and contract["path"] == "intake/trajectory.json":
                value: Any = {
                    "schema_version": "1",
                    "ontology_revision_id": context["phase_contract"]["ontology_revision_id"],
                    "targets": [
                        {
                            "repository_id": f"local:{root.name}",
                            "repository": root.name,
                            "rationale": "persona workspace",
                            "candidates": [],
                        }
                    ],
                }
                path.write_text(json.dumps(value), encoding="utf-8")
            elif contract["media_type"] == "application/json":
                path.write_text(json.dumps({"status": "simulated"}), encoding="utf-8")
            else:
                path.write_text("# Deterministic simulation artifact\n", encoding="utf-8")
    return {"status": "artifacts_written", "phase": phase, "run_id": run_id}


def _vault_credentials_assertions(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    auth_checks = context.get("auth_checks", {})
    rotation_checks = context.get("rotation_checks", {})
    visible = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (request["ledger"], request.get("report", ""))
        if path and Path(path).exists()
    )
    checks = {
        "credential.api_key_authenticates": auth_checks.get("api_key", False),
        "credential.basic_authenticates": auth_checks.get("basic", False),
        "credential.bearer_authenticates": auth_checks.get("bearer", False),
        "credential.oauth2_client_credentials_authenticates": auth_checks.get(
            "oauth2_client_credentials", False
        ),
        "credential.oauth2_refresh_token_authenticates": auth_checks.get(
            "oauth2_refresh_token", False
        ),
        "credential.jwt_bearer_authenticates": auth_checks.get("jwt_bearer", False),
        "credential.env_reference_resolves": context.get("env_credential_matches", False),
        "vault.rotation_takes_effect": rotation_checks.get("bearer", False)
        and rotation_checks.get("oauth2_client_credentials", False),
        "credentials.never_exposed": "oci_vault_proof_" not in visible,
        "cloud_vault.rotation_takes_effect": context.get("cloud_rotation_verified", False),
        "cloud_vault.grants_survive_rotation": context.get("cloud_grants_survive", False),
        "cloud_session.expired_token_auto_refreshes": context.get("cloud_session_refreshed", False),
    }
    expected = request.get("expected", [])
    failed = [name for name in expected if not checks.get(name, False)]
    if failed:
        raise ExecutionError(f"failed durability assertions: {', '.join(failed)}")
    context["final_state"] = {"status": "passed", "checks": checks}
    _write(root, context)
    return {
        "status": "passed",
        "assertions": [{"name": name, "passed": checks[name]} for name in expected],
    }


def _assertions(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    context = _read(root)
    if "vault_credentials" in context:
        return _vault_credentials_assertions(root, context, request)
    if context.get("email_proof") is not None:
        proof = context["email_proof"]
        events = proof.get("events", [])
        logs = [event for event in events if event.get("event_type") == "console.logged"]
        usage = proof.get("usage", [])
        checks = {
            "email.ingress_processed": proof.get("ingress_status") == "processed",
            "email.triggered_exactly_once": proof.get("invocation_count") == 1
            and len([event for event in events if event.get("event_type") == "trigger.received"])
            == 1,
            "email.artifacts_encrypted": int(proof.get("artifact_count", 0)) >= 2,
            "email.usage_metered": {item.get("meter") for item in usage}
            >= {"email_inbound_message", "email_inbound_chunk", "email_outbound_recipient"},
            "email.cost_attributed": all(
                item.get("internal_cost_usd") is not None
                and item.get("customer_charge_usd") is not None
                for item in usage
            ),
            "email.content_not_exposed": "This generated message" not in json.dumps(proof),
            "workflow.completed": proof.get("invocation_status") == "completed",
            "workflow.receipt_logged": len(logs) == 1,
            "recovery.is_bounded": int(request.get("recoveries", 0)) == 0,
        }
        expected = request.get("expected", [])
        failed = [name for name in expected if not checks.get(name, False)]
        if failed:
            raise ExecutionError(f"failed durability assertions: {', '.join(failed)}")
        context["final_state"] = {
            "status": proof["status"],
            "proof_id": proof["proof_id"],
            "usage": usage,
        }
        _write(root, context)
        return {
            "status": "passed",
            "assertions": [{"name": name, "passed": checks[name]} for name in expected],
        }
    run_id = context["run_id"]
    run = json.loads((root / ".outcomeci/outcomes" / run_id / "run.json").read_text())
    token = str(resolve_vault(root, "vault:simulation/api_token"))
    visible = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (request["ledger"],)
        if Path(path).exists()
    )
    interaction_paths = list(
        (root / ".outcomeci/outcomes" / run_id / "interactions").rglob("*.json")
    )
    checks = {
        "workflow.compiles": bool(compile_workflow(root / "outcome.yml")),
        "vault.decrypts_after_restart": bool(token),
        "credentials.never_exposed": token not in visible,
        "integration.uses_vault_reference": context.get("integration_verified") is True,
        "completed_phases.are_unique": len(run.get("completed_phases", []))
        == len(set(run.get("completed_phases", []))),
        "human_responses.persist": any(
            json.loads(path.read_text()).get("status") == "approved" for path in interaction_paths
        ),
        "artifacts.match_contracts": all(
            phase in run.get("completed_phases", []) for phase in ("intake", "plan", "tasks")
        ),
        "recovery.is_bounded": 0 < int(request.get("recoveries", 0)) <= 2,
        "final_status.ready_for_implementation": run.get("status") == "ready_for_implementation",
    }
    expected = request.get("expected", [])
    failed = [name for name in expected if not checks.get(name, False)]
    if failed:
        raise ExecutionError(f"failed durability assertions: {', '.join(failed)}")
    context["final_state"] = {
        "status": run["status"],
        "phase": run["phase"],
        "completed_phases": run["completed_phases"],
    }
    _write(root, context)
    return {
        "status": "passed",
        "assertions": [{"name": name, "passed": checks[name]} for name in expected],
    }


def execute(action: str, root: Path, request: dict[str, Any], fault: bool) -> dict[str, Any]:
    context = _read(root)
    if action == "workspace.initialize":
        created = initialize(root, "filesystem")
        _write(root, {"created": created})
        return {"status": "initialized", "files_created": len(created)}
    if action == "vault.initialize":
        result = initialize_vault(root)
        return {
            "status": "initialized",
            "algorithm": "AES-256-GCM",
            "vault_created": result["initialized"],
        }
    if action == "vault.put":
        token = f"oci_sim_{os.urandom(24).hex()}"
        put_vault(root, "simulation/api_token", token)
        context["canary_sha256"] = hashlib.sha256(token.encode()).hexdigest()
        _write(root, context)
        return {"status": "stored", "path": "simulation/api_token"}
    if action == "workflow.configure":
        _configure(root)
        return {"status": "configured"}
    if action == "workflow.validate":
        result = validate(root)
        return {"status": "valid", "workflow_revision": result["workflow_revision"]}
    if action == "integration.execute":
        result = _integration(root)
        context["integration_verified"] = True
        _write(root, context)
        return result
    if action == "outcome.begin":
        result = begin(root, root / "outcome.yml", str(request["intent"]))
        context["run_id"] = result["run_id"]
        _write(root, context)
        return {"status": result["status"], "run_id": result["run_id"]}
    if action == "outcome.execute":
        result = _materialize(root, str(request["phase"]))
        if fault:
            os._exit(FAULT_EXIT)
        validated = validate_artifacts(root, root / "outcome.yml", context["run_id"])
        return {"status": validated["status"], "phase": validated["phase"]}
    if action == "human.respond":
        run = json.loads(
            (root / ".outcomeci/outcomes" / context["run_id"] / "run.json").read_text()
        )
        result = respond(
            root,
            root / "outcome.yml",
            context["run_id"],
            run["pending_interaction"]["id"],
            "Approved by simulated requester.",
            approve=True,
        )
        return {"status": result["status"]}
    if action == "cloud.authenticate":
        api_url = os.environ.get("OUTCOMECI_PROOF_API_URL", "").strip()
        api_key = os.environ.get("OUTCOMECI_PROOF_API_KEY", "").strip()
        workspace_id = os.environ.get("OUTCOMECI_PROOF_WORKSPACE_ID", "").strip()
        if not api_url or not api_key or not workspace_id:
            raise ExecutionError(
                "email proof requires OUTCOMECI_PROOF_API_URL, OUTCOMECI_PROOF_API_KEY, and OUTCOMECI_PROOF_WORKSPACE_ID"
            )
        login_with_key(api_url, api_key)
        context["workspace_id"] = workspace_id
        _write(root, context)
        return {"status": "authenticated", "workspace_id": workspace_id}
    if action == "workflow.configure_email":
        _configure_email(root)
        return {"status": "configured"}
    if action == "workflow.sync":
        compiled = compile_workflow(root / "outcome.yml")
        result = sync_workflow(
            root / "outcome.yml",
            context["workspace_id"],
            None,
            "create",
        )
        context["workflow_revision"] = compiled["workflow_revision"]
        _write(root, context)
        return {"status": "synchronized", "revision": result.get("revision")}
    if action == "email.send":
        result = start_email_trigger_proof(context["workspace_id"])
        context["email_proof_id"] = result["proof_id"]
        _write(root, context)
        return {"status": result["status"], "proof_id": result["proof_id"]}
    if action == "email.wait":
        timeout = int(request.get("timeout_seconds", 180))
        deadline = time.monotonic() + min(max(timeout, 1), 600)
        while time.monotonic() < deadline:
            result = get_email_trigger_proof(context["workspace_id"], context["email_proof_id"])
            if result.get("status") in {"completed", "failed"}:
                context["email_proof"] = result
                _write(root, context)
                if result["status"] == "failed":
                    raise ExecutionError("email trigger proof failed")
                return {
                    "status": "completed",
                    "artifact_count": result.get("artifact_count"),
                    "usage_meters": [item["meter"] for item in result.get("usage", [])],
                }
            time.sleep(2)
        raise ExecutionError("email trigger proof timed out")
    if action == "console.log":
        proof = context.get("email_proof") or {}
        event = next(
            (
                item
                for item in proof.get("events", [])
                if item.get("event_type") == "console.logged"
            ),
            None,
        )
        if event is None:
            raise ExecutionError("email receipt log is unavailable")
        payload = event.get("payload", {})
        print(
            f"email received proof={proof.get('proof_id')} "
            f"attachments={payload.get('attachment_count', 0)}"
        )
        return {"status": "logged", "message": "email received"}
    if action == "outcome.advance":
        result = advance(root, root / "outcome.yml", context["run_id"], True)
        if result["phase"] != request["phase"]:
            raise ExecutionError(f"expected to advance to {request['phase']}")
        return {"status": result["status"], "phase": result["phase"]}
    if action == "vault.put_credential":
        result = credentials.put_credential(root, context, request)
        _write(root, context)
        return result
    if action == "vault.rotate_credential":
        result = credentials.rotate_credential(root, context, request)
        _write(root, context)
        return result
    if action == "vault.resolve_credential":
        result = credentials.resolve_credential(root, context, request)
        _write(root, context)
        return result
    if action == "vault.generate_jwt_credential":
        result = credentials.generate_jwt_credential(root, context, request)
        _write(root, context)
        return result
    if action == "connection.authenticate":
        result = credentials.authenticate_connection(root, request)
        checks = "rotation_checks" if request.get("after_rotation") else "auth_checks"
        context.setdefault(checks, {})[str(request["auth_type"])] = True
        _write(root, context)
        return result
    if action == "credential.resolve_env":
        result = credentials.resolve_env_credential(request)
        context["env_credential_matches"] = result["matches"]
        _write(root, context)
        return result
    if action == "cloud.mock_session":
        result = cloud_vault.mock_session(root)
        context.setdefault("vault_credentials", {})
        _write(root, context)
        return result
    if action == "cloud.vault_put":
        result = cloud_vault.vault_put(root, context, request)
        context["cloud_session_refreshed"] = True
        _write(root, context)
        return result
    if action == "cloud.vault_rotate":
        result = cloud_vault.vault_rotate(root, context, request)
        _write(root, context)
        return result
    if action == "cloud.vault_verify":
        result = cloud_vault.vault_verify(root, context, request)
        context["cloud_rotation_verified"] = result["current_value_matches"]
        context["cloud_grants_survive"] = result["workflow_ids"] == request.get(
            "expected_workflow_ids", result["workflow_ids"]
        )
        _write(root, context)
        return result
    if action == "simulation.assert":
        return _assertions(root, request)
    raise ExecutionError(f"unsupported simulation action {action}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input", default="{}")
    parser.add_argument("--fault-after-write", action="store_true")
    args = parser.parse_args()
    try:
        result = execute(
            args.action, args.workspace.resolve(), json.loads(args.input), args.fault_after_write
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
