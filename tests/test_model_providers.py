from __future__ import annotations

import json
import traceback
from dataclasses import FrozenInstanceError

import httpx
import pytest
import yaml

from outcomeci.reasoning import models
from outcomeci.reasoning.providers import (
    CHAT_PROVIDERS,
    completion_options,
    parse_model,
    provider_for_model,
)
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import ConfigError, compile_workflow

TOOL = {
    "type": "function",
    "function": {
        "name": "return_result",
        "description": "Return the answer",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
}


def workflow(tmp_path, profile, *, review=False):
    doc = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "secrets": {"model": "vault:model/api-key"},
        "reasoning": {"review" if review else "model": profile},
        "steps": [
            {"work": {"reason": "Answer the request", **({} if review else {"using": "model"})}}
        ],
    }
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.mark.parametrize("provider", CHAT_PROVIDERS)
def test_byok_profiles_compile_and_nonplatform_require_key(tmp_path, provider):
    spec = CHAT_PROVIDERS[provider]
    compile_workflow(workflow(tmp_path, {"model": spec.example_model, "key": "secrets.model"}))
    if not spec.platform_key:
        with pytest.raises(ConfigError, match="explicit key"):
            compile_workflow(workflow(tmp_path, {"model": spec.example_model}))
        with pytest.raises(ConfigError, match="explicit key"):
            compile_workflow(
                workflow(
                    tmp_path, {"model": "openai/gpt-4.1", "fallback": {"model": spec.example_model}}
                )
            )
        with pytest.raises(ConfigError, match="review"):
            compile_workflow(
                workflow(
                    tmp_path, {"model": spec.example_model, "key": "secrets.model"}, review=True
                )
            )
        with pytest.raises(ExecutionError, match="explicit Vault key"):
            models._key({"model": spec.example_model}, None)


@pytest.mark.parametrize(
    "model",
    [
        "openai/https://example.com/model",
        "openai/a?api_key=x",
        "openai/a#fragment",
        "openai/a\\b",
        "openai/a/../b",
        "openai/a//b",
        "openai/.hidden",
        "openai/" + "a" * 256,
        "azure/gpt-4",
        "bedrock/anthropic.claude",
        "vertex_ai/gemini",
        "typesafe/jev-latest",
    ],
)
def test_unreviewed_routes_and_unsafe_model_ids_are_rejected(model):
    with pytest.raises(ValueError):
        provider_for_model(model)


def test_registry_preserves_namespaces_variants_and_immutability():
    assert parse_model("openrouter/anthropic/claude-sonnet-4.5:beta") == (
        "openrouter",
        "anthropic/claude-sonnet-4.5:beta",
    )
    with pytest.raises(TypeError):
        CHAT_PROVIDERS["private"] = CHAT_PROVIDERS["openai"]
    with pytest.raises(FrozenInstanceError):
        CHAT_PROVIDERS["openai"].api_base = "https://bad.example"
    assert completion_options("gemini/gemini-3.1-pro-preview")["api_base"].endswith("/v1alpha")
    assert completion_options("gemini/gemini-2.5-flash")["api_base"].endswith("/v1beta")


def response_for(provider):
    if provider == "anthropic":
        return {
            "id": "message-test",
            "type": "message",
            "role": "assistant",
            "model": "claude",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-test",
                    "name": "return_result",
                    "input": {"answer": "yes"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    if provider == "gemini":
        return {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "return_result", "args": {"answer": "yes"}}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        }
    if provider == "cohere_chat":
        return {
            "id": "response-test",
            "finish_reason": "TOOL_CALL",
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-test",
                        "type": "function",
                        "function": {"name": "return_result", "arguments": '{"answer":"yes"}'},
                    }
                ],
            },
            "usage": {"tokens": {"input_tokens": 10, "output_tokens": 5}},
        }

    return {
        "id": "chat-test",
        "service_tier": "on_demand" if provider == "groq" else "default",
        "object": "chat.completion",
        "created": 1,
        "model": "model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if provider != "perplexity" else "stop",
                "message": {
                    "role": "assistant",
                    "content": "yes" if provider == "perplexity" else None,
                    **(
                        {}
                        if provider == "perplexity"
                        else {
                            "tool_calls": [
                                {
                                    "id": "call-test",
                                    "type": "function",
                                    "function": {
                                        "name": "return_result",
                                        "arguments": '{"answer":"yes"}',
                                    },
                                }
                            ]
                        }
                    ),
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.parametrize("provider", CHAT_PROVIDERS)
def test_real_sdk_routes_byok_and_roundtrips_tools(monkeypatch, provider):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    pytest.importorskip("litellm")
    spec = CHAT_PROVIDERS[provider]
    requests = []

    def send(client, request, **kwargs):
        requests.append(request)
        return httpx.Response(200, request=request, json=response_for(provider))

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setenv("OPENAI_API_BASE", "https://untrusted.example")
    monkeypatch.setenv(f"{provider.upper()}_API_BASE", "https://untrusted.example")
    compiled = {
        "reasoning": {"model": {"model": spec.example_model, "credential": "vault:model/api-key"}}
    }
    result = models.local_client(compiled, lambda reference: "test-vault-key")(
        step="work",
        profile="model",
        messages=[{"role": "user", "content": "Answer yes"}],
        tools=[TOOL] if spec.supports_tools else [],
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == httpx.URL(spec.api_base).host
    assert (
        "test-vault-key" in list(request.headers.values())
        or request.headers.get("authorization") == "Bearer test-vault-key"
    )
    assert "test-vault-key" not in str(request.url)
    body = json.loads(request.content)
    if provider == "deepseek":
        assert body["max_tokens"] <= 8192
    if provider == "gemini":
        assert "/models/gemini-2.5-flash:generateContent" in request.url.path
    else:
        assert body["model"] == spec.example_model.split("/", 1)[1]
    if spec.supports_tools:
        call = result["message"]["tool_calls"][0]
        assert call["name"] == "return_result"
        assert json.loads(call["arguments"]) == {"answer": "yes"}
    else:
        assert result["message"]["content"] == "yes"


def test_perplexity_tools_fail_before_provider_request(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    pytest.importorskip("litellm")
    monkeypatch.setattr(
        httpx.Client, "send", lambda *a, **k: pytest.fail("unsupported tools reached HTTP")
    )
    compiled = {
        "reasoning": {"model": {"model": "perplexity/sonar", "credential": "vault:model/key"}}
    }
    with pytest.raises(ExecutionError, match="unsupported_tools"):
        models.local_client(compiled, lambda reference: "test-key")(
            step="work",
            profile="model",
            messages=[{"role": "user", "content": "Answer"}],
            tools=[TOOL],
        )


@pytest.mark.parametrize("error_name", ["RateLimitError", "BadRequestError"])
def test_provider_errors_do_not_expose_keys_or_prompts(monkeypatch, error_name):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    litellm = pytest.importorskip("litellm")

    def fail(**kwargs):
        raise getattr(litellm, error_name)(
            message="private-vault-key private-prompt",
            model="deepseek-chat",
            llm_provider="deepseek",
        )

    monkeypatch.setattr(litellm, "completion", fail)
    compiled = {
        "reasoning": {"model": {"model": "deepseek/deepseek-chat", "credential": "vault:model/key"}}
    }
    key, prompt = "private-vault-key", "private-prompt"
    with pytest.raises(ExecutionError) as error:
        models.local_client(compiled, lambda reference: key)(
            step="work",
            profile="model",
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
    assert error_name in str(error.value)
    assert "private-vault-key" not in str(error.value)
    assert "private-prompt" not in str(error.value)
    rendered = "".join(traceback.format_exception(error.value))
    assert key not in rendered
    assert prompt not in rendered
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"max_output_tokens": None},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": True},
        {"max_output_tokens": float("inf")},
        {"max_output_tokens": 8192.0},
        {"max_output_tokens": "8192"},
    ],
)
def test_unknown_or_invalid_model_limits_use_conservative_bound(monkeypatch, metadata):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    from outcomeci.reasoning import capabilities as model_capabilities
    from outcomeci.reasoning.providers import completion_max_tokens

    monkeypatch.setattr(
        model_capabilities,
        "_catalog",
        lambda: (
            {"deepseek/new-model": {"litellm_provider": "deepseek", **metadata}},
            "1.104.2",
            "known",
        ),
    )
    assert completion_max_tokens("deepseek/new-model") == 4096
    assert completion_max_tokens("deepseek/new-model", 2048) == 2048


def test_known_and_unavailable_model_limits(monkeypatch):
    from outcomeci.reasoning import capabilities as model_capabilities
    from outcomeci.reasoning.providers import completion_max_tokens

    assert completion_max_tokens("deepseek/deepseek-chat") <= 8192
    monkeypatch.setattr(
        model_capabilities,
        "_catalog",
        lambda: (
            {"deepseek/deepseek-chat": {"litellm_provider": "deepseek", "max_output_tokens": 1024}},
            "1.104.2",
            "known",
        ),
    )
    assert completion_max_tokens("deepseek/deepseek-chat") == 1024
    assert completion_max_tokens("deepseek/unknown") == 4096
