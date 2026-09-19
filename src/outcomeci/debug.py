"""Run a backend: outcomeci workflow locally against real cloud vault credentials.

For debugging a cloud run without waiting on a merge-build-deploy cycle: this
issues a short-lived vault lease scoped to one workflow (gated server-side on
the same permission as managing that workflow's vault grants) and executes it
through the same local.trigger() path the ECS runner itself uses, with full,
unredacted output in this terminal.
"""

from __future__ import annotations

import json
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cloud import complete_debug_lease, issue_debug_lease
from .config import compile_workflow
from .process import ExecutionError


def _lease_resolver(values: dict[str, Any], expires_at: str):
    expires = datetime.fromisoformat(expires_at)

    def resolver(reference: str) -> Any:
        if datetime.now(UTC) >= expires:
            raise ExecutionError("debug credential lease expired; run the command again")
        if not reference.startswith("vault:"):
            raise ExecutionError("cloud credentials must use vault references")
        path = reference.removeprefix("vault:")
        if path not in values:
            raise ExecutionError(
                f"credential {path!r} is not granted to this workflow; "
                "grant it with `oci vault grant`"
            )
        return values[path]

    return resolver


def _synthesize_payload(trigger_name: str, definition: dict[str, Any]) -> dict[str, Any]:
    trigger_type = definition["type"]
    if trigger_type == "manual":
        return {}
    if trigger_type == "cron":
        return {
            "schema_version": "outcomeci.trigger.cron/v1",
            "type": "cron",
            "schedule_id": str(uuid.uuid4()),
            "generation": 1,
            "schedule_arn": "arn:debug:local:schedule",
            "scheduled_at": datetime.now(UTC).isoformat(),
            "execution_id": f"debug-{uuid.uuid4()}",
            "attempt_number": 1,
            "trigger_name": trigger_name,
        }
    raise ExecutionError(
        f"cannot synthesize a payload for trigger type {trigger_type!r}; pass --payload"
    )


def run(
    root: Path,
    config: Path,
    workspace_id: str,
    workflow_id: str,
    *,
    trigger_name: str | None = None,
    invocation_id: str | None = None,
    payload_path: Path | None = None,
    agent: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    from . import local

    compiled = compile_workflow(config)
    lease = issue_debug_lease(workspace_id, workflow_id, invocation_id=invocation_id)
    resolver = _lease_resolver(lease["values"], lease["expires_at"])

    if invocation_id is not None:
        name = lease.get("trigger_name")
        if not name:
            raise ExecutionError(
                "this invocation has no recorded trigger; it may predate this contract"
            )
        payload = lease.get("input") or {}
    else:
        name = trigger_name
        if not name:
            raise ExecutionError("pass --trigger <name>, or --run <invocation-id> to replay one")
        definition = compiled["triggers"].get(name)
        if definition is None:
            raise ExecutionError(f"workflow does not declare trigger {name}")
        payload = (
            json.loads(payload_path.read_text(encoding="utf-8"))
            if payload_path is not None
            else _synthesize_payload(name, definition)
        )

    print(
        f"Debugging trigger {name!r} locally against workspace {workspace_id}, "
        f"workflow {workflow_id}, using real cloud vault credentials.",
        file=sys.stderr,
    )
    try:
        result = local.trigger(
            root,
            config,
            name,
            payload,
            agent=agent,
            model=model,
            credential_resolver=resolver,
            execution_backend="outcomeci",
            _container_isolated=False,
        )
    except Exception:
        if invocation_id is not None:
            with suppress(ExecutionError):
                complete_debug_lease(workspace_id, workflow_id, invocation_id, "failed")
        raise
    if invocation_id is not None:
        complete_debug_lease(workspace_id, workflow_id, invocation_id, "completed")
    return result
