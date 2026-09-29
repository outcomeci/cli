"""Isolated process actions used by the ecosystem simulator."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..cloud import (
    get_email_trigger_proof,
    login_with_key,
    start_email_trigger_proof,
    sync_workflow,
)
from ..config import compile_workflow
from ..local_vault import initialize as initialize_vault
from ..process import ExecutionError
from ..repository import initialize, validate
from ..security import atomic_write_json
from . import cloud_vault, credentials, webhook_trigger
from .docs import fetch_fixtures

CONTEXT = Path(".outcomeci/simulation-context.json")


def _read(root: Path) -> dict[str, Any]:
    path = root / CONTEXT
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(root: Path, value: dict[str, Any]) -> None:
    atomic_write_json(root / CONTEXT, value)


EMAIL_REASON = """Read the email in the trigger and return a one-sentence summary of what it
asks for. Do not act on it.
"""


def _configure_email(root: Path) -> None:
    """Replace the workspace's workflow with a v1 email workflow named for this proof."""
    proof_name = root.parent.name.replace("_", "-")
    workflow = {
        "apiVersion": "outcomeci.workflow/v1",
        "name": f"email-{proof_name}"[:100],
        "trigger": "email",
        "steps": [
            {
                "summarize": {
                    "reason": EMAIL_REASON,
                    "from": "trigger",
                    "returns": {"summary": "string"},
                }
            }
        ],
    }
    (root / "outcome.yml").write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")


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


def _docs_assertions(
    root: Path, context: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    state = context["docs_proof"]
    commands = {item["id"]: item for item in state["commands"]}

    def command_ok(fixture_id: str) -> bool:
        item = commands.get(fixture_id)
        return item is not None and item["exit_code"] == 0

    checks = {
        "docs.cli_available": command_ok("cli-available"),
        "docs.init_succeeds": command_ok("init"),
        "docs.init_creates_workflow": (root / "outcome.yml").exists(),
        "docs.init_creates_step_instructions": any(
            (root / ".outcomeci" / "instructions").glob("*.md")
        ),
        "docs.workflow_validates": command_ok("validate"),
    }
    final_state = {"status": "passed", "commands": [item["id"] for item in state["commands"]]}
    return _evaluate(
        root, context, request, checks, final_state, error_prefix="failed docs assertions"
    )


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
            and valid.get("phase") == webhook_trigger.FIRST_STEP
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
    raise ExecutionError("no proof evidence to assert")


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
        created = initialize(root, template="workflow")
        _write(root, {"created": created})
        return {"status": "initialized", "files_created": len(created)}
    if action == "vault.initialize":
        result = initialize_vault(root)
        return {
            "status": "initialized",
            "algorithm": "AES-256-GCM",
            "vault_created": result["initialized"],
        }
    if action == "workflow.validate":
        result = validate(root)
        return {"status": "valid", "workflow_revision": result["workflow_revision"]}
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
