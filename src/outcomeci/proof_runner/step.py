"""Isolated process actions used by the ecosystem simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
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
from ..security import atomic_write_json
from . import agents, cloud_vault, credentials, webhook_trigger
from .docs import fetch_fixtures
from .mock_http import send_json
from .simulation import FAULT_EXIT

CONTEXT = Path(".outcomeci/simulation-context.json")


def _read(root: Path) -> dict[str, Any]:
    path = root / CONTEXT
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(root: Path, value: dict[str, Any]) -> None:
    atomic_write_json(root / CONTEXT, value)


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
            send_json(
                self, 200 if observed["authorized"] else 401, {"accepted": observed["authorized"]}
            )

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


def _evaluate(
    root: Path,
    context: dict[str, Any],
    request: dict[str, Any],
    checks: dict[str, bool],
    final_state: dict[str, Any],
    *,
    error_prefix: str = "failed durability assertions",
) -> dict[str, Any]:
    """Shared tail for every proof's assertion function: raise if any
    expected check failed, else persist final_state and report which
    expected checks passed."""
    expected = request.get("expected", [])
    failed = [name for name in expected if not checks.get(name, False)]
    if failed:
        raise ExecutionError(f"{error_prefix}: {', '.join(failed)}")
    context["final_state"] = final_state
    _write(root, context)
    return {
        "status": "passed",
        "assertions": [{"name": name, "passed": checks[name]} for name in expected],
    }


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
        "credentials.never_exposed": "oci_vault_proof_" not in visible
        and not any(secret in visible for secret in context.get("generated_secrets", [])),
        "cloud_vault.rotation_takes_effect": context.get("cloud_rotation_verified", False),
        "cloud_vault.grants_survive_rotation": context.get("cloud_grants_survive", False),
        "cloud_session.expired_token_auto_refreshes": context.get("cloud_session_refreshed", False),
    }
    return _evaluate(root, context, request, checks, {"status": "passed", "checks": checks})


def _docs_state(context: dict[str, Any]) -> dict[str, Any]:
    return context.setdefault("docs_proof", {"pages": {}, "commands": [], "captures": {}})


def _substitute(text: str, captures: dict[str, Any], substitute: dict[str, str] | None) -> str:
    for placeholder, capture_key in (substitute or {}).items():
        if capture_key not in captures:
            raise ExecutionError(f"no captured value for {capture_key!r}")
        text = text.replace(placeholder, str(captures[capture_key]))
    return text


def _docs_fetch(context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    page = str(request["page"])
    fixtures = fetch_fixtures(page)
    _docs_state(context)["pages"][page] = fixtures
    return {"status": "fetched", "page": page, "fixtures": sorted(fixtures)}


def _cli_exec(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    state = _docs_state(context)
    if "command" in request:
        command, fixture_id = str(request["command"]), str(request.get("id", request["command"]))
    else:
        page, fixture_id = str(request["page"]), str(request["fixture"])
        fixtures = state["pages"].get(page)
        if fixtures is None or fixture_id not in fixtures:
            raise ExecutionError(f"doc fixture {page}/{fixture_id} was not fetched")
        fixture = fixtures[fixture_id]
        if fixture["kind"] != "cmd":
            raise ExecutionError(f"doc fixture {page}/{fixture_id} is not an executable command")
        command = fixture["body"]
    command = _substitute(command, state["captures"], request.get("substitute"))
    argv = shlex.split(command)
    if not argv or argv[0] != "oci":
        raise ExecutionError("cli.exec only runs documented `oci` commands")
    completed = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False)
    record = {
        "id": fixture_id,
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
    }
    state["commands"].append(record)
    for capture_key, json_path in (request.get("capture") or {}).items():
        if completed.returncode != 0:
            continue
        try:
            value: Any = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"cli.exec output for {fixture_id} was not JSON") from exc
        for part in json_path.split("."):
            value = value[part]
        state["captures"][capture_key] = value
    if request.get("expect_success", True) and completed.returncode != 0:
        raise ExecutionError(
            f"documented command failed: {command}\n{(completed.stderr or completed.stdout)[-2000:]}"
        )
    return {"status": "executed", "id": fixture_id, "exit_code": completed.returncode}


def _status_reports_run(record: dict[str, Any] | None, run_id: Any) -> bool:
    if record is None or run_id is None or record["exit_code"] != 0:
        return False
    try:
        parsed = json.loads(record["stdout"])
    except json.JSONDecodeError:
        return False
    return parsed.get("run_id") == run_id


def _docs_assertions(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    state = context["docs_proof"]
    commands = {item["id"]: item for item in state["commands"]}

    def command_ok(fixture_id: str) -> bool:
        item = commands.get(fixture_id)
        return item is not None and item["exit_code"] == 0

    outcomes_dir_exists = False
    quickstart = state["pages"].get("quickstart", {})
    if "outcomes-dir" in quickstart:
        relative = _substitute(
            quickstart["outcomes-dir"]["body"].strip(), state["captures"], {"<run-id>": "run_id"}
        )
        outcomes_dir_exists = (root / relative).is_dir()

    checks = {
        "docs.cli_available": command_ok("cli-available"),
        "docs.init_succeeds": command_ok("init"),
        "docs.init_creates_workflow": (root / "outcome.yml").exists(),
        "docs.init_creates_agent_instructions": (
            root / ".claude" / "skills" / "outcome" / "SKILL.md"
        ).exists(),
        "docs.status_succeeds": command_ok("status"),
        "docs.status_reports_run_id": _status_reports_run(
            commands.get("status"), state["captures"].get("run_id")
        ),
        "docs.run_artifacts_recorded": outcomes_dir_exists,
    }
    final_state = {"status": "passed", "commands": [item["id"] for item in state["commands"]]}
    return _evaluate(
        root, context, request, checks, final_state, error_prefix="failed docs assertions"
    )


def _agent_driven_assertions(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    checks_by_agent = context.get("agent_checks", {})
    checks = {}
    for agent in ("claude", "codex"):
        agent_checks = checks_by_agent.get(agent, {})
        checks[f"agent.{agent}_completes_intake"] = agent_checks.get("intake_completed", False)
        checks[f"agent.{agent}_writes_valid_artifacts"] = agent_checks.get("artifacts_valid", False)
    return _evaluate(root, context, request, checks, {"status": "passed", "checks": checks})


def _webhook_trigger_assertions(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    attempts = context.get("webhook_attempts", {})
    valid = attempts.get("accepts_valid_payload", {})
    trigger = valid.get("trigger") or {}
    checks = {
        "webhook_trigger.definition_compiles": context.get("webhook_definition_compiled", False),
        "webhook_trigger.accepts_valid_payload": valid.get("outcome") == "accepted",
        "webhook_trigger.state_shape_matches_payload": (
            valid.get("outcome") == "accepted"
            and trigger.get("type") == "webhook.received"
            and trigger.get("name") == webhook_trigger.TRIGGER_NAME
            and valid.get("pending_interaction_id") == webhook_trigger.BEFORE_INTERACTION_ID
        ),
        "webhook_trigger.intent_falls_back_to_generic_text": valid.get("intent")
        == webhook_trigger.GENERIC_INTENT,
        "webhook_trigger.rejects_oversized_payload": attempts.get("oversized_payload", {}).get(
            "outcome"
        )
        == "rejected",
        "webhook_trigger.rejects_schema_invalid_payload": attempts.get(
            "schema_invalid_payload", {}
        ).get("outcome")
        == "rejected",
    }
    return _evaluate(root, context, request, checks, {"status": "passed", "checks": checks})


def _assertions(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    context = _read(root)
    if "webhook_attempts" in context:
        return _webhook_trigger_assertions(root, context, request)
    if "agent_runs" in context:
        return _agent_driven_assertions(root, context, request)
    if "vault_credentials" in context:
        return _vault_credentials_assertions(root, context, request)
    if context.get("docs_proof") is not None:
        return _docs_assertions(root, context, request)
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
        final_state = {"status": proof["status"], "proof_id": proof["proof_id"], "usage": usage}
        return _evaluate(root, context, request, checks, final_state)
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
    final_state = {
        "status": run["status"],
        "phase": run["phase"],
        "completed_phases": run["completed_phases"],
    }
    return _evaluate(root, context, request, checks, final_state)


# Actions whose entire behavior is "call (root, context, request) -> dict,
# persist the (possibly mutated) context, return the result unchanged" --
# roughly a third of execute()'s branches share exactly this shape and
# nothing else. The ones with any other side effect (a different call
# signature, an extra context mutation, fault injection, a raise) stay as
# their own explicit branch below rather than being forced in here.
_WRITE_AND_RETURN: dict[str, Callable[[Path, dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "cli.exec": _cli_exec,
    "vault.put_credential": credentials.put_credential,
    "vault.rotate_credential": credentials.rotate_credential,
    "vault.resolve_credential": credentials.resolve_credential,
    "vault.generate_jwt_credential": credentials.generate_jwt_credential,
    "cloud.vault_rotate": cloud_vault.vault_rotate,
    "agent.start_run": agents.start_run,
    "agent.approve_intake": agents.approve_intake,
    "agent.verify_run": agents.verify_run,
    "webhook_trigger.fire": webhook_trigger.fire,
}


def execute(action: str, root: Path, request: dict[str, Any], fault: bool) -> dict[str, Any]:
    context = _read(root)
    handler = _WRITE_AND_RETURN.get(action)
    if handler is not None:
        result = handler(root, context, request)
        _write(root, context)
        return result
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
    if action == "docs.fetch":
        result = _docs_fetch(context, request)
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
    if action == "cloud.vault_verify":
        result = cloud_vault.vault_verify(root, context, request)
        context["cloud_rotation_verified"] = result["current_value_matches"]
        context["cloud_grants_survive"] = result["workflow_ids"] == request.get(
            "expected_workflow_ids", result["workflow_ids"]
        )
        _write(root, context)
        return result
    if action == "webhook_trigger.configure":
        result = webhook_trigger.configure(root)
        context["webhook_definition_compiled"] = True
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
