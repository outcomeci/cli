"""Declarative, persona-based ecosystem durability simulations."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from ..process import ExecutionError

REPORT_SCHEMA = "outcomeci.proof-report/v1alpha1"
FAULT_EXIT = 86
ALLOWED_ACTIONS = {
    "workspace.initialize",
    "vault.initialize",
    "vault.put",
    "workflow.configure",
    "workflow.validate",
    "integration.execute",
    "outcome.begin",
    "outcome.execute",
    "human.respond",
    "outcome.advance",
    "cloud.authenticate",
    "workflow.configure_email",
    "workflow.sync",
    "email.send",
    "email.wait",
    "console.log",
    "docs.fetch",
    "cli.exec",
    "vault.put_credential",
    "vault.rotate_credential",
    "vault.resolve_credential",
    "vault.generate_jwt_credential",
    "connection.authenticate",
    "credential.resolve_env",
    "cloud.mock_session",
    "cloud.vault_put",
    "cloud.vault_rotate",
    "cloud.vault_verify",
    "agent.start_run",
    "agent.approve_intake",
    "agent.verify_run",
    "webhook_trigger.configure",
    "webhook_trigger.fire",
}
ALLOWED_ASSERTIONS = {
    "workflow.compiles",
    "vault.decrypts_after_restart",
    "credentials.never_exposed",
    "integration.uses_vault_reference",
    "completed_phases.are_unique",
    "human_responses.persist",
    "artifacts.match_contracts",
    "recovery.is_bounded",
    "final_status.ready_for_implementation",
    "email.ingress_processed",
    "email.triggered_exactly_once",
    "email.artifacts_encrypted",
    "email.usage_metered",
    "email.cost_attributed",
    "email.content_not_exposed",
    "workflow.completed",
    "workflow.receipt_logged",
    "docs.cli_available",
    "docs.init_succeeds",
    "docs.init_creates_workflow",
    "docs.init_creates_agent_instructions",
    "docs.status_succeeds",
    "docs.status_reports_run_id",
    "docs.run_artifacts_recorded",
    "credential.api_key_authenticates",
    "credential.basic_authenticates",
    "credential.bearer_authenticates",
    "credential.oauth2_client_credentials_authenticates",
    "credential.oauth2_refresh_token_authenticates",
    "credential.jwt_bearer_authenticates",
    "credential.env_reference_resolves",
    "vault.rotation_takes_effect",
    "cloud_vault.rotation_takes_effect",
    "cloud_vault.grants_survive_rotation",
    "cloud_session.expired_token_auto_refreshes",
    "agent.claude_completes_intake",
    "agent.claude_writes_valid_artifacts",
    "agent.codex_completes_intake",
    "agent.codex_writes_valid_artifacts",
    "webhook_trigger.definition_compiles",
    "webhook_trigger.accepts_valid_payload",
    "webhook_trigger.state_shape_matches_payload",
    "webhook_trigger.intent_falls_back_to_generic_text",
    "webhook_trigger.rejects_oversized_payload",
    "webhook_trigger.rejects_schema_invalid_payload",
}


def bundled_definition(name: str = "local-first-v1") -> Path:
    return Path(str(files("outcomeci.proof_runner").joinpath(f"proofs/{name}.proof.yml")))


def load_definition(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExecutionError(f"simulation definition could not be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ExecutionError("simulation definition must be an object")
    if value.get("apiVersion") != "outcomeci.dev/v1alpha1" or value.get("kind") != "OutcomeProof":
        raise ExecutionError("unsupported proof definition")
    metadata, spec = value.get("metadata"), value.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ExecutionError("simulation metadata and spec are required")
    persona, journey = spec.get("persona"), spec.get("journey")
    if not isinstance(persona, dict) or not isinstance(journey, list) or not journey:
        raise ExecutionError("simulation requires one persona and a non-empty journey")
    seen: set[str] = set()
    for item in journey:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ExecutionError("every journey step requires an id")
        if item["id"] in seen:
            raise ExecutionError(f"duplicate journey step {item['id']}")
        seen.add(item["id"])
        if item.get("action") not in ALLOWED_ACTIONS:
            raise ExecutionError(f"unsupported simulation action {item.get('action')}")
    assertions = spec.get("assertions", [])
    unsupported = (
        set(assertions) - ALLOWED_ASSERTIONS if isinstance(assertions, list) else {"invalid"}
    )
    if unsupported:
        raise ExecutionError(f"unsupported simulation assertion {sorted(unsupported)[0]}")
    for fault in spec.get("faults", []):
        if not isinstance(fault, dict) or fault.get("inject") != "process.kill":
            raise ExecutionError("unsupported simulation fault")
        step_id = str(fault.get("after", "")).partition(".")[0]
        if step_id not in seen or fault.get("occurrence") != "first_attempt":
            raise ExecutionError("simulation fault must target a known first-attempt step")
    return value


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.previous_hash = "0" * 64

    def append(self, **event: Any) -> str:
        self.sequence += 1
        value = {
            "sequence": self.sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "previous_hash": self.previous_hash,
            **event,
        }
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        value["event_hash"] = digest
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.previous_hash = digest
        return digest


def verify_ledger(path: Path) -> str:
    previous = "0" * 64
    for sequence, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = json.loads(line)
        digest = value.pop("event_hash")
        if value.get("sequence") != sequence or value.get("previous_hash") != previous:
            raise ExecutionError("simulation ledger chain is invalid")
        actual = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if actual != digest:
            raise ExecutionError("simulation ledger event was modified")
        previous = digest
    return previous


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _step(
    workspace: Path, step: dict[str, Any], *, fault: bool, config_home: Path
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-m",
        "outcomeci.proof_runner.step",
        step["action"],
        "--workspace",
        str(workspace),
        "--input",
        json.dumps(step.get("with", {}), separators=(",", ":")),
    ]
    if fault:
        argv.append("--fault-after-write")
    source_root = str(Path(__file__).resolve().parents[2])
    inherited_pythonpath = os.environ.get("PYTHONPATH", "")
    env = {
        **os.environ,
        "OUTCOMECI_CONFIG_HOME": str(config_home),
        "PYTHONPATH": (
            f"{source_root}{os.pathsep}{inherited_pythonpath}"
            if inherited_pythonpath
            else source_root
        ),
    }
    return subprocess.run(argv, text=True, capture_output=True, env=env, check=False)


def run(
    definition: Path | None, workspace_root: Path, report_path: Path | None = None
) -> dict[str, Any]:
    definition_path = (definition or bundled_definition()).resolve()
    document = load_definition(definition_path)
    metadata, spec = document["metadata"], document["spec"]
    proof_id = f"proof_{uuid.uuid4().hex}"
    root = workspace_root.resolve() / proof_id
    workspace, evidence = root / "workspace", root / "evidence"
    workspace.mkdir(parents=True)
    config_home = root / "config"
    output = spec.get("output", {})
    ledger_path = evidence / str(output.get("ledger", "proof-events.jsonl"))
    report = (
        report_path.resolve()
        if report_path
        else evidence / str(output.get("report", "proof-report.json"))
    )
    ledger = Ledger(ledger_path)
    started = datetime.now(UTC).isoformat()
    faults = {
        str(item["after"]).partition(".")[0]
        for item in spec.get("faults", [])
        if item.get("inject") == "process.kill"
    }
    recoveries = 0
    results: list[dict[str, Any]] = []
    ledger.append(
        event="simulation_started", persona=spec["persona"]["name"], definition=str(definition_path)
    )
    try:
        for step in spec["journey"]:
            max_attempts = 2 if step["id"] in faults else 1
            for attempt in range(1, max_attempts + 1):
                inject = step["id"] in faults and attempt == 1
                ledger.append(
                    event="step_started", step_id=step["id"], action=step["action"], attempt=attempt
                )
                completed = _step(workspace, step, fault=inject, config_home=config_home)
                if inject and completed.returncode == FAULT_EXIT:
                    recoveries += 1
                    ledger.append(
                        event="fault_injected",
                        step_id=step["id"],
                        action=step["action"],
                        attempt=attempt,
                        fault="process.kill",
                    )
                    continue
                if completed.returncode:
                    raise ExecutionError(
                        f"simulation step {step['id']} failed: {(completed.stderr or completed.stdout)[-1000:]}"
                    )
                try:
                    result = json.loads(completed.stdout.splitlines()[-1])
                except (IndexError, json.JSONDecodeError) as exc:
                    raise ExecutionError(
                        f"simulation step {step['id']} returned invalid output"
                    ) from exc
                results.append(
                    {
                        "step_id": step["id"],
                        "action": step["action"],
                        "attempt": attempt,
                        "result": result,
                    }
                )
                ledger.append(
                    event="step_completed",
                    step_id=step["id"],
                    action=step["action"],
                    attempt=attempt,
                    status=result.get("status", "completed"),
                )
                break
        assertions = _step(
            workspace,
            {
                "action": "simulation.assert",
                "with": {
                    "expected": spec["assertions"],
                    "recoveries": recoveries,
                    "ledger": str(ledger_path),
                    "report": str(report),
                },
            },
            fault=False,
            config_home=config_home,
        )
        if assertions.returncode:
            raise ExecutionError(
                f"simulation assertions failed: {(assertions.stderr or assertions.stdout)[-1000:]}"
            )
        assertion_results = json.loads(assertions.stdout.splitlines()[-1])["assertions"]
        ledger.append(event="simulation_completed", status="passed")
        final_digest = verify_ledger(ledger_path)
        state = json.loads((workspace / ".outcomeci/simulation-context.json").read_text())
        value = {
            "schema_version": REPORT_SCHEMA,
            "proof_id": proof_id,
            "definition": {"name": metadata["name"], "version": metadata["version"]},
            "persona": {"name": spec["persona"]["name"], "version": spec["persona"]["version"]},
            "status": "passed",
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "run_id": state.get("run_id"),
            "recoveries": recoveries,
            "faults_injected": sorted(faults),
            "assertions": assertion_results,
            "final_state": state.get("final_state"),
            "ledger": {"path": str(ledger_path), "sha256": final_digest},
            "steps": results,
        }
        _atomic_json(report, value)
        return value
    except Exception as exc:
        ledger.append(event="simulation_failed", status="failed", error_type=type(exc).__name__)
        value = {
            "schema_version": REPORT_SCHEMA,
            "proof_id": proof_id,
            "definition": {"name": metadata.get("name"), "version": metadata.get("version")},
            "persona": {
                "name": spec["persona"].get("name"),
                "version": spec["persona"].get("version"),
            },
            "status": "failed",
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "recoveries": recoveries,
            "error": str(exc),
            "ledger": {"path": str(ledger_path), "sha256": verify_ledger(ledger_path)},
        }
        _atomic_json(report, value)
        raise
