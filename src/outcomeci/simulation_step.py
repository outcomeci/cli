"""Isolated process actions used by the ecosystem simulator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import yaml

from .config import compile_workflow
from .integrations import IntegrationExecutor, local_credential_resolver
from .local import advance, begin, compile_context, respond, validate_artifacts
from .local_vault import initialize as initialize_vault
from .local_vault import put as put_vault
from .local_vault import resolve as resolve_vault
from .process import ExecutionError
from .repository import initialize, validate
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


def _assertions(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    context = _read(root)
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
    if action == "outcome.advance":
        result = advance(root, root / "outcome.yml", context["run_id"], True)
        if result["phase"] != request["phase"]:
            raise ExecutionError(f"expected to advance to {request['phase']}")
        return {"status": result["status"], "phase": result["phase"]}
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
