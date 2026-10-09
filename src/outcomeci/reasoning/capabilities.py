"""Offline, tri-state model metadata and safe request preflight.

The installed LiteLLM catalog is evidence, not a promise of provider behavior.
Absent metadata remains unverified. Diagnostics never contain request content.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import metadata
from typing import Any

from outcomeci.reasoning.providers import CHAT_PROVIDERS, parse_model


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    provider: str
    tools: bool | None = None
    vision: bool | None = None
    mode: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    metadata_status: str = "unknown"
    metadata_source: str = "litellm_bundled_catalog"
    metadata_version: str | None = None


class CapabilityError(ValueError):
    def __init__(
        self,
        code: str,
        model: str,
        capability: str,
        *,
        step: str | None = None,
        profile: str | None = None,
        role: str | None = None,
    ):
        self.code, self.model, self.capability = code, model, capability
        self.step, self.profile, self.role = step, profile, role
        location = " ".join(
            value
            for value in (
                f"step {step}" if step else None,
                f"profile {profile}" if profile else None,
                role,
            )
            if value
        )
        super().__init__((f"{location}: " if location else "") + f"{model}: {code} ({capability})")


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict, str | None, str]:
    try:
        distribution = metadata.distribution("litellm")
    except metadata.PackageNotFoundError:
        return {}, None, "sdk_unavailable"
    try:
        path = distribution.locate_file("litellm/model_prices_and_context_window_backup.json")
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            return {}, distribution.version, "unknown"
        return (
            {key: value for key, value in content.items() if isinstance(key, str)},
            distribution.version,
            "known",
        )
    except (OSError, ValueError):
        return {}, distribution.version, "unknown"


def _positive(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def get_capabilities(model: str) -> ModelCapabilities:
    provider, suffix = parse_model(model)
    catalog, version, status = _catalog()
    info = None
    # Only exact provider-qualified metadata, or a bare key explicitly assigned
    # to that provider. Never borrow a vendor's metadata for an aggregator model.
    for key in (model, suffix):
        candidate = catalog.get(key)
        if not isinstance(candidate, dict):
            continue
        if candidate.get("litellm_provider") != provider:
            continue
        info = candidate
        break
    if info is None:
        return ModelCapabilities(
            model,
            provider,
            metadata_status="sdk_unavailable" if status == "sdk_unavailable" else "unknown",
            metadata_version=version,
        )
    return ModelCapabilities(
        model,
        provider,
        tools=info.get("supports_function_calling")
        if type(info.get("supports_function_calling")) is bool
        else None,
        vision=info.get("supports_vision") if type(info.get("supports_vision")) is bool else None,
        mode=info.get("mode") if isinstance(info.get("mode"), str) else None,
        max_input_tokens=_positive(info.get("max_input_tokens")),
        max_output_tokens=_positive(info.get("max_output_tokens")),
        metadata_status="known",
        metadata_version=version,
    )


def validate_requirements(model: str, *, tools: bool = False, vision: bool = False) -> dict:
    caps = get_capabilities(model)
    if caps.mode is not None and caps.mode != "chat":
        raise CapabilityError("incompatible_model_mode", model, "chat")
    if tools and (caps.tools is False or not CHAT_PROVIDERS[caps.provider].supports_tools):
        raise CapabilityError("unsupported_tools", model, "tools")
    if vision and caps.vision is False:
        raise CapabilityError("unsupported_vision", model, "vision")
    warnings = []
    if caps.metadata_status != "known":
        warnings.append(caps.metadata_status)
    if caps.mode is None:
        warnings.append("chat_mode_unverified")
    for required, value, name in ((tools, caps.tools, "tools"), (vision, caps.vision, "vision")):
        if required and value is None:
            warnings.append(f"{name}_unverified")
    if caps.max_input_tokens is None:
        warnings.append("input_limit_unverified")
    if caps.max_output_tokens is None:
        warnings.append("output_limit_unverified")
    return {
        "model": model,
        "status": "unverified" if warnings else "verified",
        "requirements": {"tools": tools, "vision": vision},
        "capabilities": asdict(caps),
        "warnings": warnings,
    }


def _estimate(model: str, messages: list, tools: list) -> int | None:
    """Use the SDK's token counter and packaged tokenizer, with no downloads."""
    try:
        import litellm
        from tokenizers import Tokenizer

        if getattr(litellm, "disable_token_counter", False):
            return None
        distribution = metadata.distribution("litellm")
        path = distribution.locate_file(
            "litellm/litellm_core_utils/tokenizers/anthropic_tokenizer.json"
        )
        tokenizer = Tokenizer.from_file(str(path))
        count = litellm.token_counter(
            model=model,
            messages=messages,
            tools=tools,
            custom_tokenizer={"type": "huggingface_tokenizer", "tokenizer": tokenizer},
            use_default_image_token_count=True,
        )
        return count if type(count) is int and count > 0 else None
    except Exception:
        return None


def request_requirements(messages: list, tools: list) -> dict[str, bool]:
    vision = any(
        isinstance(part, dict) and part.get("type") in {"image_url", "input_image", "image"}
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for part in message["content"]
    )
    return {"tools": bool(tools), "vision": vision}


def preflight(model: str, *, messages: list, tools: list, output_limit: int = 32768) -> dict:
    if type(output_limit) is not int or output_limit <= 0:
        raise ValueError("output_limit must be a positive integer")
    report = validate_requirements(model, **request_requirements(messages, tools))
    caps = report["capabilities"]
    count = _estimate(model, messages, tools)
    maximum = min(output_limit, caps["max_output_tokens"] or 4096)
    if count is not None and caps["max_input_tokens"] is not None:
        remaining = caps["max_input_tokens"] - count
        if remaining <= 0:
            raise CapabilityError("estimated_input_exceeds_window", model, "input_tokens")
        maximum = min(maximum, remaining)
    report["estimated_input_tokens"] = count
    report["input_count_status"] = "estimated" if count is not None else "unverified"
    report["max_tokens"] = maximum
    report["warnings"].append(
        "input_tokens_estimated" if count is not None else "tokenizer_unavailable"
    )
    # A portable tokenizer provides an estimate, never a verified provider count.
    report["status"] = "unverified"
    return report


def workflow_report(compiled: dict) -> list[dict]:
    reports = []
    for name, step in compiled.get("instructions", {}).get("steps", {}).items():
        block = step.get("v1") or {}
        profile = block.get("reasoning")
        if block.get("kind") not in {"agent", "converse"} or not isinstance(profile, dict):
            continue
        needs_tools = bool(
            block.get("grants") or block.get("returns") or block.get("kind") == "converse"
        )
        choices = [("primary", profile)] + (
            [("fallback", profile["fallback"])] if profile.get("fallback") else []
        )
        for role, choice in choices:
            try:
                report = validate_requirements(choice["model"], tools=needs_tools)
            except CapabilityError as exc:
                raise CapabilityError(
                    exc.code,
                    exc.model,
                    exc.capability,
                    step=name,
                    profile=profile["profile"],
                    role=role,
                ) from None
            reports.append({**report, "step": name, "profile": profile["profile"], "role": role})
    return reports


def flatten_warnings(reports: list[dict]) -> list[str]:
    return [
        f"{report.get('step', 'model')}/{report.get('role', 'primary')} ({report['model']}): {warning}"
        for report in reports
        for warning in report["warnings"]
    ]
