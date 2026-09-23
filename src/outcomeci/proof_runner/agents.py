"""Drive a real Claude Code or Codex session through the local `/outcome`
flow, for agent-driven-v1.

Every other proof in this package is deterministic and free: they simulate
phase execution (local-first-v1) or drive the real CLI without ever
launching a model (docs-quickstart-v1, vault-credentials-v1). This one is
different on purpose — it's the only proof that answers "does a real agent,
not our own simulation of one, actually complete the flow our docs
describe." That means it needs a real ANTHROPIC_API_KEY/
CLAUDE_CODE_OAUTH_TOKEN or OPENAI_API_KEY (or an already-authenticated local
`claude`/`codex` CLI), costs real money per run, takes real wall-clock time,
and is not deterministic run to run. It is intentionally not wired into the
always-on CI job — run it explicitly with `oci proof run --name
agent-driven-v1` when you want this specific guarantee checked.

Scoped to the intake phase only: one real model call per agent, writing one
contracted artifact, then a local (non-Slack) approval gate. No API
integrations or Slack hooks are in play, so a failure here means the
agent/harness contract broke, not that a dependency flaked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..local import respond, start
from ..process import ExecutionError

INTAKE_APPROVAL_ID = "confirm_intent"


def _agent_state(context: dict[str, Any]) -> dict[str, Any]:
    return context.setdefault("agent_runs", {})


def start_run(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    agent = str(request["agent"])
    intent = str(request["intent"])
    result = start(root, root / "outcome.yml", intent, agent=agent)
    _agent_state(context)[agent] = {"run_id": result["run_id"]}
    return {"status": result["status"], "run_id": result["run_id"], "agent": agent}


def approve_intake(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    agent = str(request["agent"])
    tracked = _agent_state(context).get(agent)
    if tracked is None:
        raise ExecutionError(f"no run was started for agent {agent}")
    result = respond(
        root,
        root / "outcome.yml",
        tracked["run_id"],
        INTAKE_APPROVAL_ID,
        f"Approved by agent-driven-v1 for {agent}.",
        approve=True,
    )
    return {"status": result["status"], "agent": agent}


def verify_run(root: Path, context: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    agent = str(request["agent"])
    tracked = _agent_state(context).get(agent)
    if tracked is None:
        raise ExecutionError(f"no run was started for agent {agent}")
    run_id = tracked["run_id"]
    run = json.loads((root / ".outcomeci/outcomes" / run_id / "run.json").read_text())
    intake_completed = "intake" in run.get("completed_phases", [])
    trajectory_path = root / ".outcomeci/outcomes" / run_id / "intake/trajectory.json"
    artifacts_valid = False
    if trajectory_path.exists():
        try:
            trajectory = json.loads(trajectory_path.read_text())
            targets = trajectory.get("targets")
            artifacts_valid = (
                trajectory.get("schema_version") == "1"
                and bool(trajectory.get("ontology_revision_id"))
                and isinstance(targets, list)
                and len(targets) > 0
                and all(
                    isinstance(target, dict)
                    and target.get("repository_id")
                    and target.get("repository")
                    and target.get("rationale")
                    and isinstance(target.get("candidates"), list)
                    for target in targets
                )
            )
        except json.JSONDecodeError:
            artifacts_valid = False
    context.setdefault("agent_checks", {})[agent] = {
        "intake_completed": intake_completed,
        "artifacts_valid": artifacts_valid,
    }
    return {
        "status": "verified",
        "agent": agent,
        "intake_completed": intake_completed,
        "artifacts_valid": artifacts_valid,
    }
