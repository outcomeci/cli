"""Reviewed API-key chat providers shared by authoring, local runs, and Cloud.

No SDK import or provider detection is needed to validate a workflow. Endpoints
are fixed here; workflow documents and ambient SDK configuration cannot redirect
Vault keys. Platform-managed keys are limited to OpenAI and Anthropic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModelProvider:
    api_base: str
    platform_key: bool = False
    env_key: str | None = None
    example_model: str = ""
    supports_tools: bool = True


PLATFORM_PROVIDERS = frozenset({"openai", "anthropic"})
REVIEW_PROVIDERS = PLATFORM_PROVIDERS
DECISION_PROVIDERS = frozenset({"openai", "typesafe"})
CHAT_PROVIDERS = MappingProxyType(
    {
        "openai": ModelProvider(
            "https://api.openai.com/v1", True, "OPENAI_API_KEY", "openai/gpt-4.1-mini"
        ),
        "anthropic": ModelProvider(
            "https://api.anthropic.com", True, "ANTHROPIC_API_KEY", "anthropic/claude-sonnet-4-5"
        ),
        "gemini": ModelProvider(
            "https://generativelanguage.googleapis.com/v1beta",
            example_model="gemini/gemini-2.5-flash",
        ),
        "groq": ModelProvider(
            "https://api.groq.com/openai/v1", example_model="groq/llama-3.3-70b-versatile"
        ),
        "mistral": ModelProvider(
            "https://api.mistral.ai/v1", example_model="mistral/mistral-large-latest"
        ),
        "deepseek": ModelProvider(
            "https://api.deepseek.com/beta", example_model="deepseek/deepseek-chat"
        ),
        "xai": ModelProvider("https://api.x.ai/v1", example_model="xai/grok-3-mini"),
        "together_ai": ModelProvider(
            "https://api.together.ai/v1",
            example_model="together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ),
        "fireworks_ai": ModelProvider(
            "https://api.fireworks.ai/inference/v1",
            example_model="fireworks_ai/accounts/fireworks/models/llama-v3p3-70b-instruct",
        ),
        "cerebras": ModelProvider(
            "https://api.cerebras.ai/v1", example_model="cerebras/llama-3.3-70b"
        ),
        "openrouter": ModelProvider(
            "https://openrouter.ai/api/v1", example_model="openrouter/anthropic/claude-sonnet-4.5"
        ),
        "cohere_chat": ModelProvider(
            "https://api.cohere.com/v2/chat", example_model="cohere_chat/command-r-plus"
        ),
        "sambanova": ModelProvider(
            "https://api.sambanova.ai/v1", example_model="sambanova/Meta-Llama-3.3-70B-Instruct"
        ),
        "perplexity": ModelProvider(
            "https://api.perplexity.ai", example_model="perplexity/sonar", supports_tools=False
        ),
        "nvidia_nim": ModelProvider(
            "https://integrate.api.nvidia.com/v1",
            example_model="nvidia_nim/meta/llama-3.3-70b-instruct",
        ),
        "deepinfra": ModelProvider(
            "https://api.deepinfra.com/v1/openai",
            example_model="deepinfra/meta-llama/Llama-3.3-70B-Instruct",
        ),
    }
)
# Namespaces and provider variants are permitted, URLs and path traversal are not.
CHAT_MODEL_ID = re.compile(
    r"^[a-z][a-z0-9_]*/[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*$"
)


def parse_model(model: str) -> tuple[str, str]:
    if not isinstance(model, str) or len(model) > 256 or not CHAT_MODEL_ID.fullmatch(model):
        raise ValueError(
            "model must be a safe <provider>/<model> identifier of at most 256 characters"
        )
    provider, _, name = model.partition("/")
    if provider not in CHAT_PROVIDERS or any(part in {".", ".."} for part in name.split("/")):
        raise ValueError("unsupported chat model provider or model identifier")
    return provider, name


def provider_for_model(model: str) -> ModelProvider:
    return CHAT_PROVIDERS[parse_model(model)[0]]


def completion_options(model: str) -> dict:
    provider, name = parse_model(model)
    api_base = CHAT_PROVIDERS[provider].api_base
    if provider == "cohere_chat" and "v1/" in name:
        api_base = "https://api.cohere.ai/v1/chat"
    if provider == "gemini":
        # Match the SDK's fixed public AI Studio version selection. Gemini 3+
        # requires alpha; explicit api_base otherwise suppresses that selection.
        last = name.split("/")[-1].lower()
        old = (
            last.isdigit()
            or last.startswith("gemma-")
            or not last.startswith("gemini-")
            or re.match(
                r"^gemini-(?:[12](?:\.\d+)?|exp|(?:pro|flash)(?!-(?:lite-)?latest$))(?:-|$)", last
            )
        )
        if not old:
            api_base = "https://generativelanguage.googleapis.com/v1alpha"
    return {
        "api_base": api_base,
        "custom_llm_provider": provider,
        "num_retries": 0,
        "caching": False,
        "drop_params": False,
        "no-log": True,
    }


def completion_max_tokens(model: str, limit: int = 32768) -> int:
    """Bound requests by known model limits, conservatively when uncatalogued.

    Metadata is advisory: the provider still validates its current model limits.
    Importing this registry alone never imports LiteLLM or fetches its catalog.
    """
    provider, _ = parse_model(model)
    if type(limit) is not int or limit <= 0:
        raise ValueError("completion token limit must be a positive integer")
    import litellm

    try:
        info = litellm.get_model_info(model=model, custom_llm_provider=provider)
    except Exception:
        return min(limit, 4096)
    maximum = info.get("max_output_tokens") if isinstance(info, dict) else None
    if type(maximum) is not int or maximum <= 0:
        return min(limit, 4096)
    return min(limit, maximum)
