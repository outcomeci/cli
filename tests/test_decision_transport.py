from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from outcomeci.reasoning.decision_transport import install_decision_response_guard

litellm = pytest.importorskip("litellm")
from litellm.caching.llm_caching_handler import LLMClientCache  # noqa: E402
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler  # noqa: E402

QUESTIONS = [
    {"type": "predicate", "name": "safe", "instructions": "Is it safe?"},
    {"type": "predicate", "name": "urgent", "instructions": "Is it urgent?"},
]


def payload(provider):
    if provider == "typesafe":
        return {
            "model": "jev-latest",
            "answers": {
                "safe": {"type": "noul", "noul": 0.9},
                "urgent": {"type": "noul", "noul": 0.2},
            },
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }
    return {
        "model": "gpt-6-luna",
        "answers": [
            {"type": "predicate", "name": "safe", "probability": 0.9},
            {"type": "predicate", "name": "urgent", "probability": 0.2},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
    }


def call_sdk(monkeypatch, data, *, provider="openai", asynchronous=False, questions=None):
    """Use the actual SDK, real cached HTTP handlers, and a no-network transport."""
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=data if isinstance(data, bytes) else json.dumps(data).encode(),
            headers={"content-type": "application/json"},
        )
    )
    arguments = {
        "model": "typesafe/jev-latest" if provider == "typesafe" else "openai/gpt-6-luna",
        "api_key": "secret-test-key",
        "input": "private-input",
        "questions": questions or QUESTIONS,
        "num_retries": 0,
        "no-log": True,
    }
    cache = litellm.in_memory_llm_clients_cache
    if asynchronous:

        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                handler = AsyncHTTPHandler(transport=transport)
                await handler.client.aclose()
                handler.client = client
                cache.set_cache(key=f"async_httpx_client{provider}", value=handler)
                install_decision_response_guard(provider, asynchronous=True)
                install_decision_response_guard(provider, asynchronous=True)
                assert len(client.event_hooks["response"]) == 1
                assert len(handler.event_hooks["response"]) == 1
                return await litellm.adecisions(**arguments)

        return asyncio.run(run())
    with httpx.Client(transport=transport) as client:
        handler = HTTPHandler(client=client)
        cache.set_cache(key="httpx_client", value=handler)
        install_decision_response_guard(provider, asynchronous=False)
        install_decision_response_guard(provider, asynchronous=False)
        assert len(client.event_hooks["response"]) == 1
        return litellm.decisions(**arguments)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider", ["openai", "typesafe"])
def test_real_sdk_preserves_valid_named_answers(monkeypatch, provider, asynchronous):
    result = call_sdk(monkeypatch, payload(provider), provider=provider, asynchronous=asynchronous)
    assert result.answers[0].name == "safe"
    assert result.answers[0].probability == pytest.approx(0.9)
    assert result.answers[1].name == "urgent"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("bad", ["reordered", "missing", "extra", "duplicate", "wrong-kind"])
def test_real_openai_adapter_cannot_erase_invalid_answers(monkeypatch, bad, asynchronous):
    data = payload("openai")
    answers = data["answers"]
    if bad == "reordered":
        answers.reverse()
    elif bad == "missing":
        answers.pop()
    elif bad == "extra":
        answers.append(copy.deepcopy(answers[0]))
    elif bad == "duplicate":
        answers[1]["name"] = "safe"
    else:
        answers[0] = {"type": "choice", "name": "safe"}
    with pytest.raises(Exception, match="Invalid decision provider response") as caught:
        call_sdk(monkeypatch, data, asynchronous=asynchronous)
    assert "secret-test-key" not in str(caught.value)
    assert "private-input" not in str(caught.value)


@pytest.mark.parametrize("provider", ["openai", "typesafe"])
@pytest.mark.parametrize("bad", ["missing", "extra", "duplicate"])
def test_raw_choice_probability_keys_are_checked(monkeypatch, provider, bad):
    questions = [
        {
            "type": "choice",
            "name": "team",
            "instructions": "Choose a team",
            "choices": [{"value": "support"}, {"value": "engineering"}],
        }
    ]
    data = payload(provider)
    if provider == "openai":
        probabilities = [
            {"value": "support", "probability": 1},
            {"value": "engineering", "probability": 0},
        ]
        if bad == "missing":
            probabilities.pop()
        elif bad == "extra":
            probabilities.append({"value": "other", "probability": 0})
        else:
            probabilities[1]["value"] = "support"
        data["answers"] = [
            {
                "type": "choice",
                "name": "team",
                "choice": "support",
                "confidence": 1,
                "probabilities": probabilities,
            }
        ]
    else:
        probabilities = {"support": 1, "engineering": 0}
        if bad == "missing":
            probabilities.pop("engineering")
        elif bad == "extra":
            probabilities["other"] = 0
        data["answers"] = {
            "team": {
                "type": "choice",
                "choice": "support",
                "confidence": 1,
                "probabilities": probabilities,
            }
        }
        if bad == "duplicate":
            data = json.dumps(data).replace('"support": 1', '"support": 1, "support": 0').encode()
    with pytest.raises(Exception, match="Invalid decision provider response"):
        call_sdk(monkeypatch, data, provider=provider, questions=questions)


def test_typesafe_unknown_answer_is_not_discarded(monkeypatch):
    data = payload("typesafe")
    data["answers"]["unexpected"] = {"type": "noul", "noul": 1}
    with pytest.raises(Exception, match="Invalid decision provider response"):
        call_sdk(monkeypatch, data, provider="typesafe", asynchronous=True)


def test_async_hooks_survive_owned_client_recreation(monkeypatch):
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())

    async def run():
        handler = AsyncHTTPHandler(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        )
        litellm.in_memory_llm_clients_cache.set_cache(key="async_httpx_clientopenai", value=handler)
        install_decision_response_guard("openai", asynchronous=True)
        original = handler.client
        await original.aclose()
        recreated = handler.client
        assert recreated is not original
        assert len(recreated.event_hooks["response"]) == 1
        await recreated.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url,status",
    [
        ("https://api.openai.com/v1/chat/completions", 200),
        ("https://example.com/v1/decisions", 200),
        ("https://api.openai.com/v1/decisions", 429),
    ],
)
def test_hook_preserves_unrelated_requests_and_provider_errors(monkeypatch, url, status):
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    seen = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text="not decision JSON")
    )
    with httpx.Client(transport=transport, event_hooks={"response": [seen.append]}) as client:
        litellm.in_memory_llm_clients_cache.set_cache(
            key="httpx_client", value=HTTPHandler(client=client)
        )
        install_decision_response_guard("openai", asynchronous=False)
        response = client.post(url, content=b"not JSON")
        assert response.status_code == status
        assert seen == [response]
        assert len(client.event_hooks["response"]) == 2
