from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

import pytest
import yaml

from outcomeci.reasoning import decisions
from outcomeci.runtime import engine as local
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import ConfigError, compile_workflow

QUESTION = {
    "type": "choice",
    "instructions": "Which team should help?",
    "choices": [{"value": "support"}, {"value": "engineering"}],
}
ANSWER = {
    "name": "team",
    "type": "choice",
    "choice": "support",
    "confidence": 0.9,
    "probabilities": [
        {"value": "support", "probability": 0.9},
        {"value": "engineering", "probability": 0.1},
    ],
}
RECEIPT = {
    "run_id": "child-run",
    "workflow_id": "child",
    "workflow_revision_id": "revision",
    "status": "queued",
}


def document():
    return {
        "apiVersion": "outcomeci.workflow/v1",
        "name": "slack-router",
        "type": "dispatcher",
        "trigger": "manual",
        "reasoning": {"router": {"model": "openai/gpt-6-luna"}},
        "steps": [
            {
                "route": {
                    "decision": {"team": copy.deepcopy(QUESTION)},
                    "with": "trigger",
                    "using": "router",
                }
            },
            {
                "support": {
                    "dispatch": "support",
                    "with": "trigger",
                    "when": "route.team.choice == support",
                }
            },
            {
                "engineering": {
                    "dispatch": "engineering",
                    "with": "trigger",
                    "when": "route.team.choice == engineering",
                }
            },
        ],
    }


def write(tmp_path, doc=None):
    path = tmp_path / "router.outcome.yaml"
    path.write_text(yaml.safe_dump(doc or document()))
    return path


def test_compiler_infers_typed_decision_and_dispatch_outputs(tmp_path):
    compiled = compile_workflow(write(tmp_path))
    route = compiled["instructions"]["steps"]["route"]["v1"]
    assert route["returns"]["schema"]["properties"]["team"]["properties"]["choice"] == {
        "enum": ["support", "engineering"]
    }
    assert compiled["source"]["type"] == "dispatcher"
    doc = document()
    doc.pop("type")
    doc["steps"] = [{"work": {"reason": "Do work"}}]
    doc["trigger"] = {"type": "dispatcher", "dispatcher": "slack-router"}
    compiled = compile_workflow(write(tmp_path, doc))
    assert compiled["triggers"]["dispatcher"]["dispatcher"] == "slack-router"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("type"),
        lambda d: d.update(type="worker"),
        lambda d: d.update(trigger="dispatcher"),
        lambda d: d.update(trigger={"type": "dispatcher"}),
        lambda d: d["steps"][0]["route"].update(can=["slack.post"]),
        lambda d: d["steps"][0]["route"].update(using="review"),
        lambda d: d["reasoning"]["router"].update(model="anthropic/claude"),
        lambda d: d["reasoning"]["router"].update(model="typesafe/jev-latest"),
        lambda d: d["steps"][0]["route"]["decision"]["team"].update(
            choices=[{"value": True}, {"value": "true"}]
        ),
        lambda d: d["steps"][1]["support"].update(with_="trigger"),
        lambda d: d["steps"][1]["support"].update({"with": ["trigger"]}),
    ],
)
def test_invalid_contracts_fail_before_execution(tmp_path, mutate):
    doc = document()
    mutate(doc)
    with pytest.raises(ConfigError):
        compile_workflow(write(tmp_path, doc))


def test_typesafe_only_usable_by_decision_steps(tmp_path):
    doc = document()
    doc["secrets"] = {"jev": "vault:models/jev"}
    doc["reasoning"]["router"] = {"model": "typesafe/jev-latest", "key": "secrets.jev"}
    compile_workflow(write(tmp_path, doc))
    doc["steps"][0] = {"route": {"reason": "route", "using": "router"}}
    with pytest.raises(ConfigError, match="only supported for decision"):
        compile_workflow(write(tmp_path, doc))


def test_conditional_dispatch_runs_without_agent_and_retains_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        local, "_execute", lambda *a, **k: pytest.fail("dispatcher leased an agent")
    )
    calls = []

    def decide(**kwargs):
        calls.append(("decision", kwargs))
        return {
            "model": "openai/gpt-6-luna",
            "answers": [ANSWER],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 7, "cache_write_tokens": 2},
            },
        }

    def dispatch(**kwargs):
        calls.append(("dispatch", kwargs))
        return RECEIPT

    payload = {"event": {"text": "help"}}
    state = local.trigger(
        tmp_path,
        write(tmp_path),
        "manual",
        payload,
        options=local.ExecutionOptions(decision_client=decide, dispatch_client=dispatch),
    )
    assert state["status"] == "completed"
    assert calls == [
        ("decision", {"step": "route", "input": {"trigger": payload}}),
        ("dispatch", {"step": "support", "input": payload}),
    ]
    artifacts = tmp_path / ".outcomeci" / "outcomes" / state["run_id"]
    assert json.loads((artifacts / "route/outputs.json").read_text())["team"] == ANSWER
    assert json.loads((artifacts / "support/dispatch.json").read_text()) == RECEIPT
    assert (artifacts / "route/decision.json").exists()
    usage = json.loads((artifacts / "transcripts/route/usage.json").read_text())["records"][0]
    assert usage["input_tokens"] == 10
    assert usage["cache_read_tokens"] == 7
    assert usage["cache_write_tokens"] == 2
    assert not (artifacts / "engineering/dispatch.json").exists()


@pytest.mark.parametrize(
    "answer",
    [
        {**ANSWER, "type": "refusal"},
        {**ANSWER, "choice": "unknown"},
        {**ANSWER, "confidence": float("nan")},
        {**ANSWER, "probabilities": [ANSWER["probabilities"][0]] * 2},
        {
            **ANSWER,
            "probabilities": [
                {"value": "support", "probability": 0.1},
                {"value": "engineering", "probability": 0.1},
            ],
        },
    ],
)
def test_invalid_provider_answers_never_dispatch(tmp_path, answer):
    with pytest.raises(ExecutionError, match="decision"):
        local.trigger(
            tmp_path,
            write(tmp_path),
            "manual",
            {},
            options=local.ExecutionOptions(
                decision_client=lambda **k: {"answers": [answer]},
                dispatch_client=lambda **k: pytest.fail("invalid decision dispatched"),
            ),
        )
    assert not list(tmp_path.glob(".outcomeci/outcomes/*/route/outputs.json"))


def test_dispatch_requires_managed_client_and_retry_does_not_rerun_decision(tmp_path):
    calls = []

    def decide(**kwargs):
        calls.append(kwargs)
        return {"answers": [ANSWER]}

    path = write(tmp_path)
    with pytest.raises(ExecutionError, match="managed dispatch client"):
        local.trigger(
            tmp_path, path, "manual", {}, options=local.ExecutionOptions(decision_client=decide)
        )
    state = local.status(tmp_path, None)
    result = local.retry(
        tmp_path,
        path,
        state["run_id"],
        options=local.ExecutionOptions(decision_client=decide, dispatch_client=lambda **k: RECEIPT),
    )
    assert result["status"] == "completed"
    assert len(calls) == 1


def test_local_adapter_uses_unified_input_and_named_questions(tmp_path, monkeypatch):
    compiled = compile_workflow(write(tmp_path))
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model_dump=lambda **k: {"answers": [ANSWER]})

    from outcomeci.reasoning import decision_transport

    monkeypatch.setattr(decision_transport, "install_decision_response_guard", lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(decisions=provider))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    decisions.local_client(compiled)(step="route", input={"trigger": {"text": "help"}})
    assert calls[0]["questions"] == [{"name": "team", **QUESTION}]
    assert json.loads(calls[0]["input"]) == {"trigger": {"text": "help"}}
    assert calls[0]["timeout"] == 60
    assert calls[0]["api_base"] == "https://api.openai.com/v1"
    assert calls[0]["no-log"] is True and calls[0]["caching"] is False


def test_dispatcher_envelope_preserves_lineage_and_resolves_selected_input(tmp_path):
    from outcomeci.runtime import steps as v1_runtime
    from outcomeci.workflow.contracts import ContractError, validate_trigger_payload

    envelope = {
        "schema_version": "outcomeci.trigger.dispatcher/v1",
        "dispatcher": "slack-router",
        "type": "dispatcher",
        "event_id": "event",
        "parent_invocation_id": "parent",
        "parent_step": "support",
        "root_invocation_id": "root",
        "depth": 1,
        "payload": {"event": {"text": "help"}},
    }
    validate_trigger_payload("dispatcher", envelope)
    state = {"trigger": {"type": "dispatcher", "value": envelope}}
    assert v1_runtime.value(tmp_path, state, "trigger.event.text") == "help"
    assert v1_runtime.value(tmp_path, state, "trigger") == envelope["payload"]
    assert state["trigger"]["value"]["parent_invocation_id"] == "parent"
    with pytest.raises(ContractError):
        validate_trigger_payload("dispatcher", envelope["payload"])


def test_cloud_decision_and_dispatch_wire_contract(monkeypatch):
    from outcomeci.cloud_runner.client import CoreClient

    calls = []
    client = CoreClient("https://api.outcomeci.test", "invocation", "bootstrap", "workflow")
    monkeypatch.setattr(client, "_post", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    client.workflow_decision("lease", step="route", input={"trigger": {}})
    client.workflow_dispatch("lease", step="support", input={"text": "help"})
    assert calls[0][0] == (
        "decision",
        {"lease_token": "lease", "step": "route", "input": {"trigger": {}}},
    )
    assert calls[1][0] == (
        "dispatch",
        {"lease_token": "lease", "step": "support", "input": {"text": "help"}},
    )


@pytest.mark.parametrize("provider", ["openai", "typesafe"])
def test_real_litellm_decisions_translates_both_providers(tmp_path, monkeypatch, provider):
    import importlib

    import httpx

    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    pytest.importorskip("litellm")
    litellm = importlib.import_module("litellm")
    from litellm.caching.llm_caching_handler import LLMClientCache
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    calls = []
    response = {
        "model": "decision-model",
        "answers": [ANSWER],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    if provider == "typesafe":
        response = {
            "answers": {
                "team": {
                    "type": "choice",
                    "choice": "support",
                    "confidence": 0.9,
                    "probabilities": {"support": 0.9, "engineering": 0.1},
                }
            },
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    def reply(request):
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json=response)

    doc = document()
    doc["secrets"] = {"model": "vault:test/model"}
    doc["reasoning"]["router"] = {
        "model": ("openai/gpt-6-luna" if provider == "openai" else "typesafe/jev-latest"),
        "key": "secrets.model",
    }
    compiled = compile_workflow(write(tmp_path, doc))
    with httpx.Client(transport=httpx.MockTransport(reply)) as http_client:
        litellm.in_memory_llm_clients_cache.set_cache(
            key="httpx_client", value=HTTPHandler(client=http_client)
        )
        result = decisions.local_client(compiled, lambda ref: "test-key")(
            step="route", input={"trigger": {"text": "help"}}
        )
        assert len(http_client.event_hooks["response"]) == 1
    assert decisions.validate_answers({"team": QUESTION}, result)["team"]["choice"] == "support"
    assert result["usage"]["input_tokens"] == 10
    if provider == "openai":
        assert calls[0][0].endswith("/v1/decisions")
        assert calls[0][1]["questions"][0]["name"] == "team"
    else:
        assert calls[0][0].endswith("/v1/systemone")
        assert calls[0][1]["questions"]["team"]["type"] == "choice"


@pytest.mark.parametrize(
    "question,answer",
    [
        (
            {"type": "predicate", "instructions": "Is this support?"},
            {"type": "predicate", "name": "result", "probability": 0.8},
        ),
        (
            {
                "type": "score",
                "instructions": "How urgent?",
                "levels": [{"label": "low"}, {"label": "high"}],
            },
            {
                "type": "score",
                "name": "result",
                "score": 0.8,
                "confidence": 0.8,
                "probabilities": [
                    {"value": 0, "label": "low", "probability": 0.2},
                    {"value": 1, "label": "high", "probability": 0.8},
                ],
            },
        ),
    ],
)
def test_predicate_and_score_answers_preserve_typed_evidence(question, answer):
    assert decisions.validate_answers({"result": question}, {"answers": [answer]}) == {
        "result": answer
    }
    with pytest.raises(ExecutionError):
        decisions.validate_answers({"result": question}, {"answers": [{**answer, "name": "other"}]})


@pytest.mark.parametrize(
    "model", ["openai/gpt-5.5", "openai/unknown-model", "openai/gpt-6-luna-unknown"]
)
def test_unsupported_openai_decision_model_rejected(tmp_path, model):
    doc = document()
    doc["reasoning"]["router"]["model"] = model
    with pytest.raises(ConfigError, match="require openai/gpt-6-luna"):
        compile_workflow(write(tmp_path, doc))
