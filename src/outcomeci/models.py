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
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from .execution_events import safe_text
from .integrations import CredentialResolver
from .model_capabilities import (
    CapabilityError,
    flatten_warnings,
    preflight,
    request_requirements,
    validate_requirements,
)
from .model_providers import completion_options, provider_for_model
from .process import ExecutionError

MAX_TOOL_CALLS = 20
# The longest reply one turn may produce. A step's result travels as the
# arguments of one tool call, so this bounds how much a step can return.
MAX_OUTPUT_TOKENS = 32768
# Long enough for a reply at the output limit from a slow provider.
TURN_TIMEOUT_SECONDS = 600
# The LiteLLM errors that mean the provider, not the request, is the problem,
# so a profile's fallback model is worth trying.
TRANSIENT_ERRORS = (
    "RateLimitError",
    "ServiceUnavailableError",
    "InternalServerError",
    "Timeout",
    "APIConnectionError",
)
RESULT = "return_result"
# The longest tool result a turn carries back to the model, in characters. One
# page of a search, such as 30 X posts with their authors, is about 32,000.
TOOL_RESULT_LIMIT = 120_000
# A turn resends the conversation, images included, and OutcomeCI Cloud takes
# at most 8 MiB per turn: three images of at most 1.5 MiB each fit.
IMAGE_LIMIT = 3 * 512 * 1024
MAX_IMAGES = 3
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

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
        spec = compiled.get("reasoning", {}).get(profile)
        if not spec or "model" not in spec:
            raise ExecutionError(f"step {step}: reasoning profile {profile} is not a model")
        # The profile's model first; on a provider that is rate limited, down,
        # or unreachable, the profile's fallback model, once.
        choices = [spec, *([spec["fallback"]] if spec.get("fallback") else [])]
        for index, choice in enumerate(choices):
            try:
                validate_requirements(choice["model"], **request_requirements(messages, tools))
            except CapabilityError as exc:
                raise ExecutionError(
                    str(
                        CapabilityError(
                            exc.code,
                            exc.model,
                            exc.capability,
                            step=step,
                            profile=profile,
                            role="primary" if index == 0 else "fallback",
                        )
                    )
                ) from None
        try:
            import litellm
        except ImportError as exc:
            raise ExecutionError(
                "a model step on your machine needs LiteLLM: pip install 'outcomeci-cli[models]'"
            ) from exc

        reports = []
        for index, choice in enumerate(choices):
            role = "primary" if index == 0 else "fallback"
            try:
                report = preflight(
                    choice["model"], messages=messages, tools=tools, output_limit=MAX_OUTPUT_TOKENS
                )
            except CapabilityError as exc:
                raise ExecutionError(
                    str(
                        CapabilityError(
                            exc.code,
                            exc.model,
                            exc.capability,
                            step=step,
                            profile=profile,
                            role=role,
                        )
                    )
                ) from None
            reports.append({**report, "step": step, "profile": profile, "role": role})
        transient = tuple(
            getattr(litellm, name)
            for name in TRANSIENT_ERRORS
            if isinstance(getattr(litellm, name, None), type)
        )
        for index, chosen in enumerate(choices):
            model = str(chosen["model"])
            try:
                supports_tools = provider_for_model(model).supports_tools
            except ValueError as exc:
                raise ExecutionError(str(exc)) from exc
            if tools and not supports_tools:
                raise ExecutionError(
                    f"step {step}: {model.split('/')[0]} does not support tools or typed returns"
                )
            turn_messages, turn_tools = (
                cached(messages, tools) if model.startswith("anthropic/") else (messages, tools)
            )
            try:
                response = litellm.completion(
                    model=model,
                    api_key=_key(chosen, resolver),
                    messages=turn_messages,
                    **({"tools": turn_tools} if turn_tools else {}),
                    max_tokens=reports[index]["max_tokens"],
                    timeout=TURN_TIMEOUT_SECONDS,
                    **completion_options(model),
                )
                break
            except transient as exc:
                if index == len(choices) - 1:
                    raise ExecutionError(
                        f"step {step}: {model} is unavailable ({type(exc).__name__})"
                    ) from None
            except ExecutionError:
                raise
            except Exception as exc:
                raise ExecutionError(
                    f"step {step}: {model} provider call failed ({type(exc).__name__})"
                ) from None
        choice = response.choices[0]
        fallback_from = str(choices[0]["model"]) if index else None
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
            "usage": _usage(getattr(response, "usage", None)),
            "model": model,
            "model_capabilities": reports,
            "capability_warnings": flatten_warnings(reports),
            "provider": model.split("/", 1)[0],
            "fallback_from": fallback_from,
            # Which credential paid: a Vault key the profile names, or the
            # provider's environment variable. Never the key itself.
            "credential": {"source": "vault" if chosen.get("credential") else "environment"},
        }

    return call


CACHE_CONTROL = {"type": "ephemeral"}


def cached(messages: list[dict], tools: list[dict]) -> tuple[list[dict], list[dict]]:
    """Copies of a turn's messages and tools with Anthropic prompt-cache
    breakpoints on what every turn of the step resends: the system prompt,
    the tool definitions, and the first user message with the step's context.
    Writing a segment costs a quarter more than sending it and reading it back
    costs a tenth, so a segment pays only once a later turn reuses it. The
    static prefix is reused by every turn after the first; the tool results a
    turn adds are usually sent once more at most, so they are left unmarked.
    OpenAI caches a repeated prefix on its own and takes no marks. The
    caller's lists are left alone, so marks never accumulate over turns."""

    def marked(message: dict) -> dict:
        content = message.get("content")
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list) and content:
            blocks = [dict(block) for block in content]
        else:
            return message
        blocks[-1] = {**blocks[-1], "cache_control": CACHE_CONTROL}
        return {**message, "content": blocks}

    if all(item.get("function", {}).get("name") == RESULT for item in tools):
        # A step with nothing to call but return_result usually ends in one
        # turn, and a segment written once and never read costs more than
        # sending it plain.
        return messages, tools
    result = list(messages)
    first_user = next((i for i, m in enumerate(result) if m.get("role") == "user"), -1)
    for index, message in enumerate(result):
        if message.get("role") == "system" or index == first_user:
            result[index] = marked(message)
    marked_tools = list(tools)
    if marked_tools:
        marked_tools[-1] = {**marked_tools[-1], "cache_control": CACHE_CONTROL}
    return result, marked_tools


def _usage(usage: Any) -> dict[str, int]:
    """The tokens one turn used, in the fields every agent's usage records share."""
    if usage is None:
        return {}

    def field(*names: str) -> int:
        for name in names:
            value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
            if isinstance(value, int | float):
                return int(value)
        return 0

    details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage, Mapping)
        else getattr(usage, "prompt_tokens_details", None)
    )
    cached = 0
    if details is not None:
        cached = (
            details.get("cached_tokens")
            if isinstance(details, Mapping)
            else getattr(details, "cached_tokens", None)
        ) or 0
    # Accepts a record already in the shared shape as well, so normalizing a
    # local turn's usage a second time keeps its cache counts.
    return {
        "input_tokens": field("prompt_tokens", "input_tokens"),
        "output_tokens": field("completion_tokens", "output_tokens"),
        "cache_read_tokens": int(cached)
        or field("cache_read_input_tokens", "cache_read_tokens", "cached_tokens"),
        "cache_write_tokens": field(
            "cache_creation_input_tokens", "cache_write_tokens", "cache_write_input_tokens"
        ),
    }


def write_transcript(root: Path, step: str, provider: str, turns: list[dict[str, Any]]) -> dict:
    """Keep a model step's turns beside an agent's transcripts, with the same
    usage.json an agent step gets, so a run shows what the model saw and spent."""
    target = root / "transcripts" / step / "model"
    target.mkdir(parents=True, exist_ok=True)
    transcript = target / "01-turns.jsonl"
    transcript.write_text(
        "".join(json.dumps(turn, default=str) + "\n" for turn in turns), encoding="utf-8"
    )
    relative = str(transcript.relative_to(root))
    records = [
        {
            "provider": provider,
            "source_line": line,
            "occurred_at": turn.get("occurred_at"),
            "transcript_path": relative,
            **turn["usage"],
        }
        for line, turn in enumerate(turns, 1)
        if turn.get("usage")
    ]
    usage_path = root / "transcripts" / step / "usage.json"
    usage_path.write_text(
        json.dumps(
            {"schema_version": 1, "provider": provider, "step": step, "records": records},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "provider": provider,
        "step": step,
        "files": [{"path": relative, "byte_size": transcript.stat().st_size}],
        "usage_path": str(usage_path.relative_to(root)),
        "usage_records": len(records),
        "usage": records,
    }


def _key(spec: Mapping[str, Any], resolver: CredentialResolver | None) -> str:
    if spec.get("credential"):
        if resolver is None:
            raise ExecutionError(f"no credential resolver for {spec['credential']}")
        value = _api_key(resolver(spec["credential"]))
        if not value:
            raise ExecutionError(f"{spec['credential']} holds no API key")
        return value
    provider = str(spec["model"]).split("/", 1)[0]
    try:
        definition = provider_for_model(str(spec["model"]))
    except ValueError as exc:
        raise ExecutionError(str(exc)) from exc
    if not definition.platform_key or not definition.env_key:
        raise ExecutionError(f"{provider} requires an explicit Vault key")
    value = os.environ.get(definition.env_key, "")
    if not value:
        raise ExecutionError(f"set {definition.env_key}, or give the profile a key: secrets.<name>")
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
    turns: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Run one model step to its result: (the result or None, the model's last text).

    `call(capability, inputs)` executes a capability through the broker. A
    result that does not match `returns` goes back to the model with the
    reason, within the same budget of tool calls. Each turn is appended to
    `turns` as it happens, so a transcript survives however the step ends."""
    allowed = {tool_name(item["capability"]): item["capability"] for item in capabilities}
    offered = tools(capabilities, returns)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": [{"type": "text", "text": user}, *(images or [])]}
        if images
        else {"role": "user", "content": user},
    ]
    if turns is not None:
        turns.append(
            {
                "turn": 0,
                "occurred_at": datetime.now(UTC).isoformat(),
                "system": system,
                "user": user,
                "tools": [item["function"]["name"] for item in offered],
                "images": len(images or []),
            }
        )
    used, nudged, text = 0, False, ""
    shown = len(images or [])
    while True:
        started = time.monotonic()
        try:
            answer = client(step=step, profile=profile, messages=messages, tools=offered)
        except Exception as exc:
            # A turn that failed is part of the record too: what was asked,
            # when, and why it failed (the provider's text never gets here).
            if turns is not None:
                turns.append(
                    {
                        "turn": len(turns),
                        "occurred_at": datetime.now(UTC).isoformat(),
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "error": safe_text(str(exc), 500),
                    }
                )
            raise
        if turns is not None:
            latency = answer.get("latency_ms")
            turns.append(
                {
                    "turn": len(turns),
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "finish_reason": answer.get("finish_reason"),
                    # A cloud turn reports usage in the provider's own field
                    # names; a local one already in the shared ones.
                    "usage": _usage(answer.get("usage")) if answer.get("usage") else {},
                    "model": answer.get("model"),
                    "model_capabilities": answer.get("model_capabilities", []),
                    "capability_warnings": answer.get("capability_warnings", []),
                    "provider": answer.get("provider"),
                    "fallback_from": answer.get("fallback_from"),
                    # The provider's own time when OutcomeCI Cloud reports it,
                    # otherwise the turn's round trip as the runner saw it.
                    "latency_ms": latency
                    if isinstance(latency, int)
                    else round((time.monotonic() - started) * 1000),
                    "credential": answer.get("credential"),
                    "assistant": answer.get("message") or {},
                    "tool_results": [],
                }
            )
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
                said = f"; it said: {text.strip()[:300]!r}" if text and text.strip() else ""
                raise ExecutionError(
                    f"step {step}: the model finished without calling {RESULT}{said}"
                )
            nudged = True
            messages.append({"role": "user", "content": f"Call {RESULT} with the step's result."})
            continue
        seen: list[dict] = []

        def reply(item: Mapping[str, Any], result: Any) -> None:
            message = _tool(item, result)
            messages.append(message)
            if turns is not None:
                turns[-1]["tool_results"].append(
                    {"id": item["id"], "name": item.get("name"), "content": message["content"]}
                )

        for item in calls:
            used += 1
            if used > MAX_TOOL_CALLS:
                raise ExecutionError(f"step {step}: more than {MAX_TOOL_CALLS} tool calls")
            try:
                arguments = json.loads(item.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as exc:
                reply(item, {"error": f"invalid arguments: {exc}"})
                continue
            if item["name"] == RESULT and returns:
                try:
                    jsonschema.validate(arguments, returns)
                except jsonschema.ValidationError as exc:
                    reply(item, {"error": f"result is invalid: {exc.message}"})
                    continue
                return arguments, text
            capability = allowed.get(item["name"])
            if capability is None:
                reply(item, {"error": f"{item['name']} is not a tool here"})
                continue
            try:
                result = call(capability, arguments)
            except ExecutionError as exc:
                result = {"error": str(exc)}
            reply(item, result)
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
        # Cutting the JSON itself hands the model a document it cannot parse;
        # wrap the cut so what it receives is still one valid object.
        content = json.dumps(
            {"truncated": True, "limit": TOOL_RESULT_LIMIT, "partial": content[:TOOL_RESULT_LIMIT]},
            separators=(",", ":"),
        )
    return {"role": "tool", "tool_call_id": item["id"], "content": content}
