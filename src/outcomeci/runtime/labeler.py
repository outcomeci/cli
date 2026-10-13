"""The training labeler: one model call over a finished run that annotates it.

A workflow that declares `training.labeler` gets an annotation written beside
its run record once the run completes. The annotation is a separate record,
as the episode plan requires: it never changes the run's result, and a
labeler that fails is recorded as failed rather than failing the run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outcomeci.reasoning import models
from outcomeci.runtime.events import safe_text
from outcomeci.security import atomic_write_json
from outcomeci.workflow import templates

RUBRIC_VERSION = 1
OUTPUT_LIMIT = 4000
ANNOTATION = "training/annotation.json"

LABELS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["success", "partial", "failure"],
            "description": "Whether the run achieved what its steps set out to do.",
        },
        "failure_category": {
            "type": "string",
            "description": "For a partial or failed run, one short category (snake_case).",
        },
        "labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short typed labels describing the run.",
        },
        "explanation": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["step", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["outcome", "labels", "explanation", "evidence"],
    "additionalProperties": False,
}


def declared(compiled: dict[str, Any]) -> dict[str, Any] | None:
    labeler = (compiled.get("training") or {}).get("labeler")
    return labeler if isinstance(labeler, dict) else None


def _outputs(outcome_root: Path, step: str) -> Any:
    path = outcome_root / step / "outputs.json"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if len(text) > OUTPUT_LIMIT:
        return {"truncated": True, "text": safe_text(text, OUTPUT_LIMIT)}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": safe_text(text, OUTPUT_LIMIT)}


def record(compiled: dict[str, Any], state: dict[str, Any], outcome_root: Path) -> dict[str, Any]:
    """What the labeler sees: the run's shape and each step's result, never
    credentials or transcripts."""
    steps = compiled["instructions"]["steps"]
    skip_reasons = state.get("skip_reasons") or {}
    return {
        "workflow": compiled.get("workflow", {}).get("metadata", {}).get("name"),
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "summary": state.get("summary"),
        "steps": [
            {
                "name": name,
                "state": (state.get("steps") or {}).get(name),
                **({"skipped": skip_reasons[name]} if name in skip_reasons else {}),
                **(
                    {"outputs": outputs}
                    if (outputs := _outputs(outcome_root, name)) is not None
                    else {}
                ),
            }
            for name in steps
        ],
    }


def label_run(root: Path, compiled: dict[str, Any], state: dict[str, Any], options) -> None:
    """Annotate a completed run when its workflow declares a labeler.

    Writes `training/annotation.json` and the labeler's transcript beneath the
    run, and records the outcome under state["training"]["labeler"]. Never
    raises: labeling is optional and must not change the run's result."""
    labeler = declared(compiled)
    if labeler is None or state.get("status") != "completed":
        return
    outcome_root = root / ".outcomeci" / "outcomes" / state["run_id"]
    started = datetime.now(UTC).isoformat()
    model = str(labeler.get("model", ""))
    provider = model.split("/")[0] or "model"
    client = options.model_client or models.local_client(compiled, options.credential_resolver)
    turns: list[dict[str, Any]] = []
    entry: dict[str, Any] = {"model": model, "rubric_version": RUBRIC_VERSION}
    try:
        try:
            result, _text = models.run(
                client,
                step=models.LABELER_PROFILE,
                profile=models.LABELER_PROFILE,
                turns=turns,
                system=compiled["instructions"]["orchestrator"]["content"]
                + "\n\n## Training labeler\n\nYou annotate finished runs of this workflow.",
                user=templates.LABELER_TASK.format(
                    result=models.RESULT,
                    record_json=json.dumps(
                        record(compiled, state, outcome_root), separators=(",", ":")
                    ),
                ),
                capabilities=[],
                call=lambda capability, inputs: {"error": f"{capability} is not a tool here"},
                returns=LABELS_SCHEMA,
            )
        finally:
            transcript = models.write_transcript(
                outcome_root, models.LABELER_PROFILE, provider, turns
            )
        annotation = {
            "schema_version": 1,
            "run_id": state["run_id"],
            "workflow_revision": compiled.get("workflow_revision"),
            "labeler": {
                "model": model,
                "profile": models.LABELER_PROFILE,
                "rubric_version": RUBRIC_VERSION,
            },
            "created_at": datetime.now(UTC).isoformat(),
            "transcript": transcript["files"][0]["path"] if transcript["files"] else None,
            **(result or {}),
        }
        atomic_write_json(outcome_root / ANNOTATION, annotation)
        entry.update(
            {
                "status": "completed",
                "path": ANNOTATION,
                "outcome": annotation["outcome"],
                "usage_records": transcript["usage_records"],
            }
        )
    except Exception as exc:  # noqa: BLE001 - labeling never fails the run
        entry.update({"status": "failed", "error": safe_text(str(exc), 500)})
    entry.update({"started_at": started, "finished_at": datetime.now(UTC).isoformat()})
    state.setdefault("training", {})["labeler"] = entry
