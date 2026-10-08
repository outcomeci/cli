"""Validate named decision answers before LiteLLM normalizes provider responses.

LiteLLM 1.104.2 rewrites OpenAI names positionally and reconstructs probability
maps. TODO: remove this transport guard when the SDK validates those raw fields.
The hook keeps the SDK's request, translation, usage, and exception handling.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx

_LOCK = threading.Lock()
_ENDPOINTS = {
    ("api.openai.com", "/v1/decisions"): "openai",
    ("api.typesafe.ai", "/v1/systemone"): "typesafe",
}
_ERROR = "Invalid decision provider response"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_ERROR)
        result[key] = value
    return result


def _value(value: Any) -> tuple[type, str | bool]:
    if type(value) not in (str, bool):
        raise ValueError(_ERROR)
    return type(value), value


def _openai(request: dict, response: dict) -> None:
    questions, answers = request["questions"], response["answers"]
    if not isinstance(questions, list) or not isinstance(answers, list):
        raise ValueError(_ERROR)
    if len(questions) != len(answers):
        raise ValueError(_ERROR)
    names = set()
    for question, answer in zip(questions, answers, strict=True):
        name = question["name"]
        if name in names or answer["name"] != name:
            raise ValueError(_ERROR)
        names.add(name)
        kind = answer["type"]
        if kind == "refusal":
            continue  # The caller rejects refusals after retaining evidence.
        if kind != question["type"]:
            raise ValueError(_ERROR)
        if kind == "choice":
            expected = {_value(option["value"]) for option in question["choices"]}
            actual = [_value(option["value"]) for option in answer["probabilities"]]
            if len(actual) != len(expected) or set(actual) != expected:
                raise ValueError(_ERROR)
        elif kind == "score":
            expected_levels = {
                (index, level["label"]) for index, level in enumerate(question["levels"])
            }
            actual_levels = []
            for option in answer["probabilities"]:
                if type(option["value"]) is not int:
                    raise ValueError(_ERROR)
                actual_levels.append((option["value"], option["label"]))
            if len(actual_levels) != len(expected_levels) or set(actual_levels) != expected_levels:
                raise ValueError(_ERROR)


def _typesafe(request: dict, response: dict) -> None:
    questions, answers = request["questions"], response["answers"]
    if not isinstance(questions, dict) or not isinstance(answers, dict):
        raise ValueError(_ERROR)
    if set(questions) != set(answers):
        raise ValueError(_ERROR)
    for name, question in questions.items():
        answer = answers[name]
        if answer["type"] != question["type"]:
            raise ValueError(_ERROR)
        if question["type"] in {"choice", "score"}:
            expected = (
                set(question["criteria"])
                if question["type"] == "choice"
                else {str(index) for index in range(len(question["criteria"]))}
            )
            actual = answer["probabilities"]
            if not isinstance(actual, dict) or set(actual) != expected:
                raise ValueError(_ERROR)


def _provider(response: httpx.Response) -> str | None:
    url = response.request.url
    if url.scheme != "https" or not response.is_success:
        return None
    return _ENDPOINTS.get((url.host, url.path))


def _validate(response: httpx.Response, provider: str) -> None:
    try:
        request = json.loads(response.request.content)
        payload = json.loads(response.content, object_pairs_hook=_unique_object)
        if provider == "openai":
            _openai(request, payload)
        else:
            _typesafe(request, payload)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        # Never include response bodies, request evidence, or credentials in errors.
        raise ValueError(_ERROR) from None


def _sync_response(response: httpx.Response) -> None:
    provider = _provider(response)
    if provider:
        response.read()
        _validate(response, provider)


async def _async_response(response: httpx.Response) -> None:
    provider = _provider(response)
    if provider:
        await response.aread()
        _validate(response, provider)


def install_decision_response_guard(provider: str, *, asynchronous: bool) -> None:
    """Install once per cached client; call immediately before each SDK request.

    Hooks examine their own request, so concurrent calls share no question state.
    Async handler hooks survive client recreation. The sync handler has no hook
    configuration; accessing its client here heals it before registering hooks.
    """
    from litellm.llms.custom_httpx.http_handler import (
        _get_httpx_client,
        get_async_httpx_client,
    )

    if provider not in {"openai", "typesafe"}:
        raise ValueError("Unsupported decision provider")
    with _LOCK:
        handler = get_async_httpx_client(provider) if asynchronous else _get_httpx_client()
        hook = _async_response if asynchronous else _sync_response
        client = handler.client
        responses = client.event_hooks.setdefault("response", [])
        if hook not in responses:
            responses.append(hook)
        if asynchronous:
            hooks = {key: list(value) for key, value in (handler.event_hooks or {}).items()}
            responses = hooks.setdefault("response", [])
            if hook not in responses:
                responses.append(hook)
            handler.event_hooks = hooks
