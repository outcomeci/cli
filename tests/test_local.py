from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from outcomeci.runtime import engine as local
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.scaffold import initialize


@pytest.fixture(autouse=True)
def capability_context(monkeypatch):
    monkeypatch.setattr(local, "serve_capability", lambda *args, **kwargs: nullcontext({}))


def test_continue_run_forwards_the_cloud_execution_context(tmp_path: Path, monkeypatch) -> None:
    state = {
        "run_id": "run-1",
        "status": "awaiting_confirmation",
        "completed_steps": ["resolve_analytics"],
    }
    local._write(tmp_path, state)
    monkeypatch.setattr(
        local,
        "compile_workflow",
        lambda config: {
            "instructions": {
                "steps": {
                    "resolve_analytics": {"needs": []},
                    "notify": {"needs": ["resolve_analytics"]},
                }
            }
        },
    )
    captured = {}

    def fake_execute(root, config, state, **options):
        captured.update(options)
        return {"run_id": state["run_id"], "status": "completed"}

    monkeypatch.setattr(local, "_execute", fake_execute)
    resolver = lambda reference: "value"  # noqa: E731

    result = local.continue_run(
        tmp_path,
        tmp_path / "outcome.yml",
        "run-1",
        approve=True,
        options=local.ExecutionOptions(credential_resolver=resolver, _container_isolated=True),
    )

    assert result["status"] == "completed"
    assert captured["options"].credential_resolver is resolver
    assert captured["options"]._container_isolated is True


def test_ready_set_supports_parallel_steps_and_join() -> None:
    compiled = {
        "instructions": {
            "steps": {
                "intake": {"needs": []},
                "product_review": {"needs": ["intake"]},
                "technical_review": {"needs": ["intake"]},
                "plan": {"needs": ["product_review", "technical_review"]},
            }
        }
    }
    assert local._ready(compiled, ["intake"]) == ["product_review", "technical_review"]
    assert local._ready(compiled, ["intake", "product_review"]) == ["technical_review"]
    assert local._ready(compiled, ["intake", "product_review", "technical_review"]) == ["plan"]


def test_call_succeeded_requires_broker_ok_true() -> None:
    assert local._call_succeeded({"result": {"ok": True}})
    assert not local._call_succeeded({"result": {"ok": False}})
    # A non-bool truthy "ok" (never emitted by the real broker, but the
    # field is untyped JSON) must not count as success -- this check is
    # shared by the must-confirm gate, which needs the strict reading.
    assert not local._call_succeeded({"result": {"ok": 1}})
    assert not local._call_succeeded({"result": {}})
    assert not local._call_succeeded({})


def test_call_succeeded_lets_the_providers_own_result_override_ok() -> None:
    call = {"result": {"ok": True, "output": {"result": {"ok": False}}}}
    assert not local._call_succeeded(call)


def test_write_effect_receipts_reports_provider_override_as_not_ok(tmp_path: Path) -> None:
    journal = tmp_path / ".outcomeci" / ".broker" / "run-1"
    journal.mkdir(parents=True)
    (journal / "journal.json").write_text(
        json.dumps(
            {
                "calls": {
                    "call-1": {
                        "capability": "slack.post_message",
                        "proposal_sha256": "abc",
                        "status": "confirmed",
                        "result": {
                            "ok": True,
                            "status": 200,
                            "output": {"result": {"ok": False}},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    outcome_root = tmp_path / "outcome"
    outcome_root.mkdir()
    target = local._write_effect_receipts(tmp_path, outcome_root, "run-1", "intake")
    effects = json.loads(target.read_text(encoding="utf-8"))
    assert effects["effects"] == [
        {
            "step": None,
            "capability": "slack.post_message",
            "proposal_sha256": "abc",
            "status": "confirmed",
            "ok": False,
            "http_status": 200,
        }
    ]


def test_declared_json_schema_is_enforced(tmp_path: Path) -> None:
    initialize(tmp_path)
    compiled = local.compile_workflow(tmp_path / "outcome.yml")
    contract = compiled["instructions"]["steps"]["investigate"]["expects"]["outputs"][0]
    outcome = tmp_path / ".outcomeci" / "outcomes" / "test"
    artifact = outcome / contract["path"]
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"wrong": true}')
    with pytest.raises(ExecutionError, match="failed JSON validation"):
        local._validate_outputs(compiled, outcome, "investigate")
