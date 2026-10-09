from __future__ import annotations

import json

import httpx
import pytest
import yaml

from outcomeci.broker.executor import IntegrationError, IntegrationExecutor
from outcomeci.broker.policy import PolicyExecutor
from outcomeci.cloud_runner.client import CoreClient
from outcomeci.workflow.compiler import ConfigError, compile_workflow

INPUT = {
    "query": "Python docs",
    "max_results": 3,
    "topic": "general",
    "search_depth": "basic",
    "include_domains": ["docs.python.org"],
    "exclude_domains": [],
}


def compiled(tmp_path, *, byok=False, scope=None):
    path = tmp_path / "search.outcome.yaml"
    doc = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "secrets": {"key": "vault:tavily/key"},
        "apis": {"web": {"uses": "tavily", **({"auth": "secrets.key"} if byok else {})}},
        "steps": [
            {
                "search": {
                    "reason": "Search",
                    "can": [
                        {
                            "web.search": scope
                            or {"topic": "general", "include_domains": ["docs.python.org"]}
                        }
                    ],
                }
            }
        ],
    }
    path.write_text(yaml.safe_dump(doc))
    return compile_workflow(path)


def broker(tmp_path, value, **kwargs):
    grants = value["instructions"]["steps"]["search"]["v1"]["grants"]
    grants = [{**g, "args": {k: v["literal"] for k, v in g["args"].items()}} for g in grants]
    return PolicyExecutor(
        IntegrationExecutor(value, reviewed=True, **kwargs), tmp_path / "broker", {}, grants=grants
    )


@pytest.mark.parametrize("byok", [False, True])
def test_proxy_never_resolves_credentials_and_records_distinct_read_receipts(tmp_path, byok):
    value = compiled(tmp_path, byok=byok)
    calls = []

    def proxy(**call):
        # Receipt must already exist before any network send.
        journal = json.loads((tmp_path / "broker/journal.json").read_text())
        assert call["request_id"] in journal["calls"]
        calls.append(call)
        return {
            "ok": True,
            "status": 200,
            "output": {"results": [], "usage": {"credits": 1}},
            "audit": {"credits": 1},
        }

    runtime = broker(
        tmp_path,
        value,
        connector_client=proxy,
        resolver=lambda _: pytest.fail("credential left API"),
    )
    first = runtime.execute("web.search", INPUT, step="search")
    second = runtime.execute("web.search", INPUT, step="search")
    assert first["receipt"] != second["receipt"]
    assert calls[0]["request_id"] == first["receipt"]
    assert calls[0]["input"] == INPUT
    assert len(calls[0]["request_id"]) == 64
    journal = json.loads((tmp_path / "broker/journal.json").read_text())
    assert journal["calls"][first["receipt"]]["result"]["output"]["usage"] == {"credits": 1}
    with pytest.raises(IntegrationError, match="outside this step's grants"):
        runtime.execute("web.search", {**INPUT, "include_domains": []}, step="search")
    assert len(calls) == 2


def test_platform_key_requires_managed_execution(tmp_path):
    value = compiled(tmp_path)
    auth = value["workflow"]["spec"]["connections"][0]["auth"]
    assert auth["managed"] is True
    assert "credential" not in auth
    with pytest.raises(IntegrationError, match="require a managed run"):
        broker(tmp_path, value).execute("web.search", INPUT, step="search")


def test_local_byok_sends_fixed_usage_flags_and_projects_usage(tmp_path, monkeypatch):
    monkeypatch.setattr("outcomeci.broker.executor._safe_destination", lambda *a: None)
    value = compiled(tmp_path, byok=True)

    def send(request):
        assert request.headers["Authorization"] == "Bearer private-key"
        body = json.loads(request.content)
        assert body["include_usage"] is True
        assert body["auto_parameters"] is False
        return httpx.Response(
            200, json={"results": [], "usage": {"credits": 1}, "request_id": "provider-id"}
        )

    result = broker(
        tmp_path, value, resolver=lambda _: "private-key", transport=httpx.MockTransport(send)
    ).execute("web.search", INPUT, step="search")
    assert result["output"]["usage"] == {"credits": 1}
    assert "private-key" not in json.dumps(result)


def test_tavily_rejects_dynamic_grants(tmp_path):
    with pytest.raises(ConfigError, match="Tavily grants must be static"):
        compiled(tmp_path, scope={"topic": "trigger.topic"})


def test_transport_retry_preserves_request_id():
    requests = []

    def send(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadError("lost response")
        return httpx.Response(200, json={"ok": True, "status": 200, "output": {}, "audit": {}})

    client = CoreClient(
        "https://example.test",
        "invocation",
        "bootstrap",
        "workflow",
        transport=httpx.MockTransport(send),
    )
    assert client.workflow_connector_call(
        "lease", step="search", capability="web.search", request_id="a" * 64, input=INPUT
    )["ok"]
    assert len(requests) == 2
    assert requests[0] == requests[1]


@pytest.mark.parametrize("choice", ["docs", "news", "general"])
def test_ordinary_decision_routes_exactly_one_scoped_search(tmp_path, choice):
    from test_model_steps import Model, _call

    from outcomeci.runtime import container, engine

    doc = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "apis": {"web": {"uses": "tavily"}},
        "reasoning": {
            "route": {"model": "openai/gpt-6-luna"},
            "answer": {"model": "openai/gpt-4.1-mini"},
        },
        "steps": [
            {
                "source": {
                    "using": "route",
                    "with": "trigger",
                    "decision": {
                        "source": {
                            "type": "choice",
                            "instructions": "Choose search source",
                            "choices": [{"value": name} for name in ["docs", "news", "general"]],
                        }
                    },
                }
            }
        ],
    }
    for name in ["docs", "news", "general"]:
        doc["steps"].append(
            {
                name: {
                    "when": f"source.source.choice == {name}",
                    "using": "answer",
                    "reason": "Search once",
                    "can": [
                        {
                            "web.search": {
                                "topic": "news" if name == "news" else "general",
                                "search_depth": "basic",
                                "max_results": 3,
                                "include_domains": ["docs.python.org"] if name == "docs" else [],
                                "exclude_domains": [],
                            }
                        }
                    ],
                    "returns": {"answer": "string"},
                }
            }
        )
    path = tmp_path / "search.outcome.yaml"
    path.write_text(yaml.safe_dump(doc))
    calls = []

    def proxy(**request):
        calls.append(request)
        return {"ok": True, "status": 200, "output": {"results": []}, "audit": {"credits": 1}}

    model = Model(
        {
            choice: [
                {"calls": [_call("web__search", {"query": "question"})]},
                {"calls": [_call("return_result", {"answer": "done"})]},
            ]
        }
    )

    def decide(**kwargs):
        return {
            "answers": [
                {
                    "name": "source",
                    "type": "choice",
                    "choice": choice,
                    "confidence": 1,
                    "probabilities": [
                        {"value": name, "probability": int(name == choice)}
                        for name in ["docs", "news", "general"]
                    ],
                }
            ],
            "usage": {},
        }

    state = container.execute(
        tmp_path,
        path,
        compile_workflow(path),
        "manual",
        {"question": "help"},
        options=engine.ExecutionOptions(
            credential_resolver=lambda _: pytest.fail("resolved key"),
            decision_client=decide,
            model_client=model,
            connector_client=proxy,
        ),
        auto_continue=True,
    )
    assert state["status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["step"] == choice
    assert calls[0]["input"]["topic"] == ("news" if choice == "news" else "general")
    assert calls[0]["input"]["include_domains"] == (["docs.python.org"] if choice == "docs" else [])
