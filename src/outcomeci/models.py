"""Model steps: a step reasoned by one model through tool calls.

A model step has no workspace and no agent. Its tools are the capabilities the
step was granted, each run through the same broker as an agent's calls, so
grants, policy review and the journal apply unchanged, plus `return_result`,
whose parameters are the step's `returns` schema.

A local run calls the provider through LiteLLM with the profile's key; a cloud
run sends each turn to OutcomeCI Cloud, which holds the keys and meters usage.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import jsonschema

from .integrations import CredentialResolver
from .process import ExecutionError

MAX_TOOL_CALLS = 20
# The longest reply one turn may produce. A step's result travels as the
# arguments of one tool call, so this bounds how much a step can return.
MAX_OUTPUT_TOKENS = 16384
RESULT = "return_result"
TOOL_RESULT_LIMIT = 20_000
# A turn resends the conversation, images included, and OutcomeCI Cloud takes
# at most 8 MiB per turn: three images of at most 1.5 MiB each fit.
IMAGE_LIMIT = 3 * 512 * 1024
MAX_IMAGES = 3
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# (step, profile, messages, tools) -> {"message": {"content", "tool_calls"}, ...}
ModelClient = Callable[..., dict[str, Any]]


def is_model_step(block: Mapping[str, Any]) -> bool:
    return bool(block.get("reasoning"))


def local_client(
    compiled: Mapping[str, Any], resolver: CredentialResolver | None = None
) -> ModelClient:
    """Call the provider directly, with the profile's Vault key or the
    provider's environment variable."""

    def call(*, step: str, profile: str, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        try:
            import litellm
        except ImportError as exc:
            raise ExecutionError(
                "a model step on your machine needs LiteLLM: pip install 'outcomeci-cli[models]'"
            ) from exc

        spec = compiled.get("reasoning", {}).get(profile)
        if not spec or "model" not in spec:
            raise ExecutionError(f"step {step}: reasoning profile {profile} is not a model")
        response = litellm.completion(
            model=spec["model"],
            api_key=_key(spec, resolver),
            messages=messages,
            **({"tools": tools} if tools else {}),
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=180,
            num_retries=0,
        )
        choice = response.choices[0]
        return {
            "message": {
                "content": choice.message.content,
                "tool_calls": [
                    {
                        "id": item.id,
                        "name": item.function.name,
                        "arguments": item.function.arguments,
                    }
                    for item in choice.message.tool_calls or []
                ],
            },
            "finish_reason": choice.finish_reason,
        }

    return call


def _key(spec: Mapping[str, Any], resolver: CredentialResolver | None) -> str:
    if spec.get("credential"):
        if resolver is None:
            raise ExecutionError(f"no credential resolver for {spec['credential']}")
        value = _api_key(resolver(spec["credential"]))
        if not value:
            raise ExecutionError(f"{spec['credential']} holds no API key")
        return value
    provider = str(spec["model"]).split("/", 1)[0]
    value = os.environ.get(PROVIDER_KEYS[provider], "")
    if not value:
        raise ExecutionError(
            f"set {PROVIDER_KEYS[provider]}, or give the profile a key: secrets.<name>"
        )
    return value


def _api_key(resolved: Mapping[str, Any] | str) -> str:
    """The provider key in a resolved Vault credential: a bare value, or its
    `api_key` (or `value`), inside `secrets` when the credential has them."""
    if isinstance(resolved, str):
        return resolved
    secrets = resolved.get("secrets")
    fields = secrets if isinstance(secrets, Mapping) else resolved
    return str(fields.get("api_key") or fields.get("value") or "")


def tool_name(capability: str) -> str:
    return capability.replace(".", "__", 1)


def tools(capabilities: list[Mapping[str, Any]], returns: Mapping[str, Any] | None) -> list[dict]:
    """The step's capabilities as function tools, and `return_result` when it returns."""
    result = [
        {
            "type": "function",
            "function": {
                "name": tool_name(item["capability"]),
                "description": str(item.get("description", ""))[:1024],
                "parameters": item.get("input") or {"type": "object"},
            },
        }
        for item in capabilities
    ]
    if returns:
        result.append(
            {
                "type": "function",
                "function": {
                    "name": RESULT,
                    "description": "Finish the step with its result. Call it exactly once.",
                    "parameters": returns,
                },
            }
        )
    return result


def image_parts(files: list[Mapping[str, Any]], limit: int = MAX_IMAGES) -> list[dict]:
    """Up to `limit` attached images a model can see, as data URLs; other files
    and larger images are skipped."""
    parts: list[dict] = []
    for item in files:
        if len(parts) >= limit:
            break
        path = Path(str(item.get("path", "")))
        kind = str(item.get("content_type") or item.get("mimetype") or "")
        if kind not in IMAGE_TYPES or not path.is_file() or path.stat().st_size > IMAGE_LIMIT:
            continue
        encoded = base64.b64encode(path.read_bytes()).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:{kind};base64,{encoded}"}})
    return parts


def run(
    client: ModelClient,
    *,
    step: str,
    profile: str,
    system: str,
    user: str,
    capabilities: list[Mapping[str, Any]],
    call: Callable[[str, dict[str, Any]], dict[str, Any]],
    returns: Mapping[str, Any] | None = None,
    images: list[dict] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Run one model step to its result: (the result or None, the model's last text).

    `call(capability, inputs)` executes a capability through the broker. A
    result that does not match `returns` goes back to the model with the
    reason, within the same budget of tool calls."""
    allowed = {tool_name(item["capability"]): item["capability"] for item in capabilities}
    offered = tools(capabilities, returns)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": [{"type": "text", "text": user}, *(images or [])]}
        if images
        else {"role": "user", "content": user},
    ]
    used, nudged, text = 0, False, ""
    shown = len(images or [])
    while True:
        answer = client(step=step, profile=profile, messages=messages, tools=offered)
        if answer.get("finish_reason") == "length":
            # A reply cut off mid-way is unusable: a truncated tool call is not
            # JSON, and sending it back only repeats the cut until the call
            # budget runs out. Say what happened instead.
            raise ExecutionError(
                f"step {step}: the model's reply was cut off at its output limit; "
                "have the step return less, such as a shortlist instead of every item"
            )
        message = answer.get("message") or {}
        calls = message.get("tool_calls") or []
        text = message.get("content") or text
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content"),
                **(
                    {
                        "tool_calls": [
                            {
                                "id": item["id"],
                                "type": "function",
                                "function": {"name": item["name"], "arguments": item["arguments"]},
                            }
                            for item in calls
                        ]
                    }
                    if calls
                    else {}
                ),
            }
        )
        if not calls:
            if not returns:
                return None, text
            if nudged:
                raise ExecutionError(f"step {step}: the model finished without calling {RESULT}")
            nudged = True
            messages.append({"role": "user", "content": f"Call {RESULT} with the step's result."})
            continue
        seen: list[dict] = []
        for item in calls:
            used += 1
            if used > MAX_TOOL_CALLS:
                raise ExecutionError(f"step {step}: more than {MAX_TOOL_CALLS} tool calls")
            try:
                arguments = json.loads(item.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as exc:
                messages.append(_tool(item, {"error": f"invalid arguments: {exc}"}))
                continue
            if item["name"] == RESULT and returns:
                try:
                    jsonschema.validate(arguments, returns)
                except jsonschema.ValidationError as exc:
                    messages.append(_tool(item, {"error": f"result is invalid: {exc.message}"}))
                    continue
                return arguments, text
            capability = allowed.get(item["name"])
            if capability is None:
                messages.append(_tool(item, {"error": f"{item['name']} is not a tool here"}))
                continue
            try:
                result = call(capability, arguments)
            except ExecutionError as exc:
                result = {"error": str(exc)}
            messages.append(_tool(item, result))
            file = (result.get("output") or {}).get("file") if isinstance(result, dict) else None
            if isinstance(file, dict):
                seen.append(file)
        pictures = image_parts(seen, MAX_IMAGES - shown)
        shown += len(pictures)
        if pictures:
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Attached files:"}, *pictures],
                }
            )


def _tool(item: Mapping[str, Any], result: Any) -> dict[str, Any]:
    content = json.dumps(result, separators=(",", ":"), default=str)
    if len(content) > TOOL_RESULT_LIMIT:
        content = content[:TOOL_RESULT_LIMIT] + " [truncated]"
    return {"role": "tool", "tool_call_id": item["id"], "content": content}
