"""Validation and deterministic compilation for outcome.yml."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import available_timezones

import jsonschema
import yaml

from . import __version__
from .contracts import ContractError, contract_schema, validate_contract
from .security import private_path

RUNNERS = {"codex", "claude", "opencode"}
INTERACTIONS = {"approval", "review", "consultation", "notification"}
HTTP_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
SIDE_EFFECTS = {"read", "create", "update", "delete", "execute"}
APPROVAL_POLICIES = {"none", "required", "inherit"}
IDEMPOTENCY_POLICIES = {"none", "supported", "required"}
TRIGGER_TYPES = {"manual", "email.received", "webhook.received", "cron"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


class ConfigError(ValueError):
    pass


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _non_empty_str(value: Any, message: str) -> str:
    """Validate value is a non-blank string, raising `message` verbatim if
    not -- callers keep their own field-specific wording (a workflow author
    reading "outcome.yml.trigger.timezone is required" needs that to stay
    distinct from ".subject_prefix must be non-empty"), only the identical
    isinstance+strip check they all repeated collapses to one place."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(message)
    return value


def _relative_path(root: Path, relative: Any, field: str) -> Path:
    relative = _non_empty_str(relative, f"{field} must be a non-empty path")
    path = (root / relative).resolve()
    if private_path(relative):
        raise ConfigError(f"{field} references a credential-bearing or broker-private path")
    try:
        resolved_relative = path.relative_to(root.resolve())
    except ValueError as exc:
        raise ConfigError(f"{field} escapes the repository") from exc
    if private_path(resolved_relative):
        raise ConfigError(f"{field} references a credential-bearing or broker-private path")
    return path


def _reference(
    root: Path, relative: str, field: str, *, json_value: bool = False
) -> dict[str, Any]:
    path = _relative_path(root, relative, field)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {field} {relative}: {exc}") from exc
    if not content.strip():
        raise ConfigError(f"{field} {relative} is empty")
    result: dict[str, Any] = {
        "path": relative,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
    }
    if json_value:
        try:
            result["value"] = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{field} {relative} is not valid JSON") from exc
        if not isinstance(result["value"], (dict, bool)):
            raise ConfigError(f"{field} {relative} must contain a JSON object")
        try:
            jsonschema.validators.validator_for(result["value"]).check_schema(result["value"])
        except jsonschema.SchemaError as exc:
            raise ConfigError(f"{field} {relative} is not a valid JSON Schema") from exc
    return result


def _agent_policy(value: Any, field: str) -> dict[str, Any]:
    item = _mapping(value or {}, field)
    runner, model = item.get("runner"), item.get("model")
    if runner is not None and runner not in RUNNERS:
        raise ConfigError(f"{field}.runner must be codex, claude, or opencode")
    if model is not None:
        model = _non_empty_str(model, f"{field}.model must be non-empty")
    if runner == "opencode" and (not isinstance(model, str) or not model.startswith("openrouter/")):
        raise ConfigError(f"{field}.model must use openrouter/provider/model for OpenCode")
    return {key: item[key] for key in ("runner", "model") if item.get(key) is not None}


def _contract(value: Any, field: str, *, output: bool) -> dict[str, Any]:
    item = _mapping(value, field)
    name, media_type = item.get("name"), item.get("media_type")
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ConfigError(f"{field}.name must be a valid identifier")
    if not isinstance(media_type, str) or "/" not in media_type:
        raise ConfigError(f"{field}.media_type is required")
    result = {
        "name": name,
        "media_type": media_type,
        "required": item.get("required", True),
    }
    if not isinstance(result["required"], bool):
        raise ConfigError(f"{field}.required must be true or false")
    if output:
        path = item.get("path")
        if (
            not isinstance(path, str)
            or not path.strip()
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ConfigError(f"{field}.path must remain within the run directory")
        result["path"] = Path(path).as_posix()
    else:
        result["from"] = _non_empty_str(item.get("from"), f"{field}.from is required")
    if item.get("schema") is not None:
        schema = item["schema"]
        if isinstance(schema, (dict, bool)):
            try:
                jsonschema.validators.validator_for(schema).check_schema(schema)
            except jsonschema.SchemaError as exc:
                raise ConfigError(f"{field}.schema is not a valid JSON Schema") from exc
        elif not isinstance(schema, str) or not schema.strip():
            raise ConfigError(f"{field}.schema must be a schema path or inline JSON Schema")
        result["schema"] = item["schema"]
    if isinstance(item.get("description"), str) and item["description"].strip():
        result["description"] = item["description"].strip()
    return result


def _human_interactions(value: Any, field: str) -> dict[str, list[dict[str, Any]]]:
    humans = _mapping(value or {}, field)
    unknown = set(humans) - {"before", "during", "after"}
    if unknown:
        raise ConfigError(f"{field} has unknown timing: {', '.join(sorted(unknown))}")
    result: dict[str, list[dict[str, Any]]] = {"before": [], "during": [], "after": []}
    seen: set[str] = set()
    for timing in result:
        entries = humans.get(timing, [])
        if not isinstance(entries, list):
            raise ConfigError(f"{field}.{timing} must be a list")
        for index, value in enumerate(entries):
            item = _mapping(value, f"{field}.{timing}[{index}]")
            interaction_id = item.get("id")
            if (
                not isinstance(interaction_id, str)
                or not IDENTIFIER.fullmatch(interaction_id)
                or interaction_id in seen
            ):
                raise ConfigError(
                    f"human interaction ids must be unique valid identifiers in {field}"
                )
            seen.add(interaction_id)
            participant = item.get("participant")
            if isinstance(participant, str):
                participant = {"role": participant}
            participant = _mapping(participant, f"{field}.{timing}[{index}].participant")
            _non_empty_str(
                participant.get("role"), f"{field}.{timing}[{index}].participant.role is required"
            )
            interaction = item.get("interaction")
            if interaction not in INTERACTIONS:
                raise ConfigError(f"{field}.{timing}[{index}].interaction is unsupported")
            required = item.get("required", interaction != "notification")
            if not isinstance(required, bool):
                raise ConfigError(f"{field}.{timing}[{index}].required must be true or false")
            purpose = _non_empty_str(
                item.get("purpose"), f"{field}.{timing}[{index}].purpose is required"
            )
            delivery = _mapping(
                item.get("delivery", {"type": "local"}),
                f"{field}.{timing}[{index}].delivery",
            )
            if delivery.get("type") not in {"local", "slack", "custom"}:
                raise ConfigError(f"{field}.{timing}[{index}].delivery.type is unsupported")
            if delivery.get("type") == "slack":
                mode = delivery.get("mode", "message")
                if mode not in {"message", "reaction", "reply"}:
                    raise ConfigError(f"{field}.{timing}[{index}].delivery.mode is unsupported")
                delivery["mode"] = mode
            is_reaction = delivery.get("type") == "slack" and delivery.get("mode") == "reaction"
            is_reply = delivery.get("type") == "slack" and delivery.get("mode") == "reply"
            if (delivery.get("type") == "custom" or delivery.get("type") == "slack") and (
                not is_reaction and not is_reply and not isinstance(delivery.get("connection"), str)
            ):
                raise ConfigError(f"{field}.{timing}[{index}].delivery.connection is required")
            if is_reaction:
                if interaction != "approval":
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.mode: reaction requires interaction: approval"
                    )
                if timing != "before":
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.mode: reaction is only supported for timing: before"
                    )
                unknown_reaction = set(delivery) - {
                    "type",
                    "mode",
                    "source",
                    "emoji",
                    "poll_interval_seconds",
                }
                if unknown_reaction:
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery has unknown fields: "
                        + ", ".join(sorted(unknown_reaction))
                    )
                source = delivery.get("source")
                if not isinstance(source, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_-]{0,62}\.outputs\.[a-z][a-z0-9_-]{0,62}", source
                ):
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.source must reference <phase>.outputs.<name>"
                    )
                emoji = _non_empty_str(
                    delivery.get("emoji", "+1"),
                    f"{field}.{timing}[{index}].delivery.emoji must be non-empty",
                )
                delivery["emoji"] = emoji.strip()
                poll_interval = delivery.get("poll_interval_seconds", 20)
                if not isinstance(poll_interval, int) or not 5 <= poll_interval <= 60:
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.poll_interval_seconds must be between 5 and 60"
                    )
                delivery["poll_interval_seconds"] = poll_interval
            elif is_reply:
                if interaction != "consultation":
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.mode: reply requires interaction: consultation"
                    )
                if timing != "before":
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.mode: reply is only supported for timing: before"
                    )
                unknown_reply = set(delivery) - {
                    "type",
                    "mode",
                    "source",
                    "poll_interval_seconds",
                }
                if unknown_reply:
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery has unknown fields: "
                        + ", ".join(sorted(unknown_reply))
                    )
                source = delivery.get("source")
                if not isinstance(source, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_-]{0,62}\.outputs\.[a-z][a-z0-9_-]{0,62}", source
                ):
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.source must reference <phase>.outputs.<name>"
                    )
                poll_interval = delivery.get("poll_interval_seconds", 20)
                if not isinstance(poll_interval, int) or not 5 <= poll_interval <= 60:
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.poll_interval_seconds must be between 5 and 60"
                    )
                delivery["poll_interval_seconds"] = poll_interval
            elif "on_timeout" in item:
                raise ConfigError(
                    f"{field}.{timing}[{index}].on_timeout is only supported with delivery.mode: reaction or reply"
                )
            targets = delivery.get("targets", [])
            if not isinstance(targets, list):
                raise ConfigError(f"{field}.{timing}[{index}].delivery.targets must be a list")
            normalized_targets = []
            for target_index, target_value in enumerate(targets):
                target = _mapping(
                    target_value,
                    f"{field}.{timing}[{index}].delivery.targets[{target_index}]",
                )
                if target.get("kind") not in {"user", "channel", "group"}:
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.targets[{target_index}].kind is unsupported"
                    )
                _non_empty_str(
                    target.get("name"),
                    f"{field}.{timing}[{index}].delivery.targets[{target_index}].name is required",
                )
                normalized_targets.append(
                    {
                        "kind": target["kind"],
                        "name": target["name"].strip().lstrip("@#"),
                    }
                )
            if normalized_targets:
                delivery["targets"] = normalized_targets
            wait_default = (
                {"strategy": "block", "timeout_seconds": 300}
                if is_reaction or is_reply
                else {"strategy": "ask"}
            )
            wait = _mapping(item.get("wait", wait_default), f"{field}.{timing}[{index}].wait")
            if wait.get("strategy") not in {"ask", "block", "continue"}:
                raise ConfigError(f"{field}.{timing}[{index}].wait.strategy is unsupported")
            if wait.get("timeout_seconds") is not None and (
                not isinstance(wait["timeout_seconds"], int)
                or not 0 <= wait["timeout_seconds"] <= 86400
            ):
                raise ConfigError(
                    f"{field}.{timing}[{index}].wait.timeout_seconds must be between 0 and 86400"
                )
            if is_reaction or is_reply:
                if wait.get("timeout_seconds") is None:
                    wait["timeout_seconds"] = 300
                if wait.get("strategy") != "block" or not wait["timeout_seconds"]:
                    raise ConfigError(
                        f"{field}.{timing}[{index}].delivery.mode: {'reaction' if is_reaction else 'reply'} requires "
                        "wait.strategy: block and a positive wait.timeout_seconds"
                    )
            on_timeout = item.get("on_timeout", "fail")
            if (is_reaction or is_reply) and on_timeout not in {"fail", "continue"}:
                raise ConfigError(f"{field}.{timing}[{index}].on_timeout is unsupported")
            normalized = {
                "id": interaction_id,
                "participant": participant,
                "purpose": purpose.strip(),
                "interaction": interaction,
                "required": required,
                "delivery": delivery,
            }
            if is_reaction or is_reply:
                normalized["on_timeout"] = on_timeout
            normalized["wait"] = {
                "strategy": wait["strategy"],
                **(
                    {"timeout_seconds": wait["timeout_seconds"]}
                    if wait.get("timeout_seconds") is not None
                    else {}
                ),
            }
            if timing == "during":
                availability = item.get("availability", "on_demand")
                if availability not in {"on_demand", "always"}:
                    raise ConfigError(f"{field}.{timing}[{index}].availability is unsupported")
                normalized["availability"] = availability
            result[timing].append(normalized)
    return result


def _phase_integrations(
    policy: dict[str, Any], field: str
) -> tuple[list[str], list[str], dict[str, list[dict[str, Any]]]]:
    """Normalize typed phase integrations into the existing runtime graph shape."""
    capabilities = policy.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
        isinstance(capability, str) and capability.strip() for capability in capabilities
    ):
        raise ConfigError(f"{field}.capabilities must be a list of names")
    capability_names = list(capabilities)
    required_capabilities: list[str] = []
    raw_humans = _mapping(policy.get("humans", {}), f"{field}.humans")
    human_groups: dict[str, list[Any]] = {
        timing: list(raw_humans.get(timing, [])) for timing in ("before", "during", "after")
    }
    entries = policy.get("integrations", [])
    if not isinstance(entries, list):
        raise ConfigError(f"{field}.integrations must be a list")
    for index, raw_entry in enumerate(entries):
        entry_field = f"{field}.integrations[{index}]"
        entry = _mapping(raw_entry, entry_field)
        integration_type = entry.get("type")
        if integration_type == "api":
            capability = _non_empty_str(
                entry.get("capability"), f"{entry_field}.capability is required"
            ).strip()
            capability_names.append(capability)
            required = entry.get("required", False)
            if not isinstance(required, bool):
                raise ConfigError(f"{entry_field}.required must be true or false")
            if required:
                required_capabilities.append(capability)
        elif integration_type == "human":
            timing = entry.get("timing")
            if timing not in human_groups:
                raise ConfigError(f"{entry_field}.timing must be before, during, or after")
            human_groups[timing].append(
                {key: value for key, value in entry.items() if key not in {"type", "timing"}}
            )
        else:
            raise ConfigError(f"{entry_field}.type must be api or human")
    humans = _human_interactions(human_groups, f"{field}.integrations")
    return sorted(set(capability_names)), sorted(set(required_capabilities)), humans


def _schema(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        value = {"type": "object", "additionalProperties": True}
    result = _mapping(value, field)
    try:
        jsonschema.validators.validator_for(result).check_schema(result)
    except jsonschema.SchemaError as exc:
        raise ConfigError(f"{field} is not a valid JSON Schema: {exc.message}") from exc
    return result


def _named_items(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_mapping(item, f"{field}[{index}]") for index, item in enumerate(value)]
    values = _mapping(value, field)
    return [{"ref": name, **_mapping(item, f"{field}.{name}")} for name, item in values.items()]


def _cron_expression(value: Any, field: str) -> str:
    value = _non_empty_str(value, f"{field} must be a five-field cron expression")
    fields = value.split()
    if len(fields) != 5:
        raise ConfigError(f"{field} must have five space-separated fields")
    minute, _hour, day_of_month, _month, day_of_week = fields
    if day_of_month != "*" and day_of_week != "*":
        raise ConfigError(f"{field} must leave day-of-month or day-of-week as *")
    step = re.fullmatch(r"\*/(\d+)", minute)
    if minute == "*" or (step and int(step.group(1)) < 5):
        raise ConfigError(f"{field} must not fire more than once every five minutes")
    if "," in minute:
        values = sorted(int(part) for part in minute.split(","))
        if any(b - a < 5 for a, b in zip(values, values[1:], strict=False)):
            raise ConfigError(f"{field} must not fire more than once every five minutes")
    return value.strip()


def _triggers(value: Any) -> dict[str, dict[str, Any]]:
    raw = _mapping(value, "spec.triggers")
    if not raw:
        raise ConfigError("spec.triggers must define at least one trigger")
    normalized: dict[str, dict[str, Any]] = {}
    for name, trigger_value in raw.items():
        field = f"spec.triggers.{name}"
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ConfigError("trigger names must be valid identifiers")
        trigger = _mapping(trigger_value, field)
        trigger_type = trigger.get("type")
        if trigger_type not in TRIGGER_TYPES:
            raise ConfigError(f"{field}.type is unsupported")
        if trigger_type == "webhook.received":
            from .webhooks import validate_delivery_config

            normalized[name] = {
                "type": trigger_type,
                **validate_delivery_config(trigger),
            }
            continue
        if trigger_type == "cron":
            unknown_cron = set(trigger) - {"type", "expression", "timezone"}
            if unknown_cron:
                raise ConfigError(f"{field} has unknown fields: {', '.join(sorted(unknown_cron))}")
            expression = _cron_expression(trigger.get("expression"), f"{field}.expression")
            timezone = _non_empty_str(
                trigger.get("timezone"), f"{field}.timezone is required"
            ).strip()
            if timezone not in available_timezones():
                raise ConfigError(f"{field}.timezone is not a recognized IANA time zone")
            normalized[name] = {
                "type": trigger_type,
                "expression": expression,
                "timezone": timezone,
            }
            continue
        unknown = set(trigger) - {"type", "filters"}
        if unknown:
            raise ConfigError(f"{field} has unknown fields: {', '.join(sorted(unknown))}")
        filters = _mapping(trigger.get("filters", {}), f"{field}.filters")
        if trigger_type == "manual" and filters:
            raise ConfigError(f"{field}.filters are not supported for manual triggers")
        allowed_filters = {"senders", "subject_prefix"}
        unknown_filters = set(filters) - allowed_filters
        if unknown_filters:
            raise ConfigError(
                f"{field}.filters has unknown fields: {', '.join(sorted(unknown_filters))}"
            )
        senders = filters.get("senders", [])
        if not isinstance(senders, list) or not all(
            isinstance(sender, str) and sender.strip() for sender in senders
        ):
            raise ConfigError(f"{field}.filters.senders must be a list of email addresses")
        subject_prefix = filters.get("subject_prefix")
        if subject_prefix is not None:
            _non_empty_str(subject_prefix, f"{field}.filters.subject_prefix must be non-empty")
        normalized[name] = {
            "type": trigger_type,
            **(
                {
                    "filters": {
                        **({"senders": sorted(set(senders))} if senders else {}),
                        **(
                            {"subject_prefix": subject_prefix.strip()}
                            if isinstance(subject_prefix, str)
                            else {}
                        ),
                    }
                }
                if filters
                else {}
            ),
        }
    return normalized


def _http_origin(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be an HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
    ):
        raise ConfigError(f"{field} must contain only an HTTP origin")
    return value.rstrip("/")


def _http_auth(value: Any, field: str) -> dict[str, Any]:
    auth = _mapping(value or {"type": "none"}, field)
    kind = auth.get("type", "none")
    if kind not in {"none", "api_key", "basic", "bearer", "oauth2", "oidc", "jwt_bearer"}:
        raise ConfigError(f"{field}.type is unsupported")
    result = {"type": kind}
    if kind != "none":
        result["credential"] = _non_empty_str(
            auth.get("credential"), f"{field}.credential is required"
        ).strip()
    for key in (
        "header",
        "query",
        "scheme",
        "token_url",
        "discovery_url",
        "scope",
        "audience",
        "grant_type",
        "account_id",
    ):
        if auth.get(key) is not None:
            result[key] = _non_empty_str(auth[key], f"{field}.{key} must be non-empty").strip()
    if kind == "api_key" and not (result.get("header") or result.get("query")):
        result["header"] = "Authorization"
        result["scheme"] = "Bearer"
    if kind in {"oauth2", "jwt_bearer"} and "token_url" not in result:
        raise ConfigError(f"{field}.token_url is required")
    if kind == "oidc" and "discovery_url" not in result:
        raise ConfigError(f"{field}.discovery_url is required")
    if kind in {"oauth2", "oidc"}:
        grant_type = result.get("grant_type", "client_credentials")
        if grant_type not in {"client_credentials", "account_credentials", "refresh_token"}:
            raise ConfigError(f"{field}.grant_type is unsupported")
        result["grant_type"] = grant_type
        if grant_type == "account_credentials" and "account_id" not in result:
            raise ConfigError(
                f"{field}.account_id is required for the account_credentials grant type"
            )
    return result


def _operation(value: Any, field: str) -> dict[str, Any]:
    item = _mapping(value, field)
    request = _mapping(item.get("request"), f"{field}.request")
    method = request.get("method")
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        raise ConfigError(f"{field}.request.method is unsupported")
    path = request.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ConfigError(f"{field}.request.path must be a relative absolute-path")
    response = _mapping(item.get("response", {}), f"{field}.response")
    expose = response.get("expose", {})
    if isinstance(expose, list):
        expose = {entry.rsplit(".", 1)[-1]: entry for entry in expose}
    expose = _mapping(expose, f"{field}.response.expose")
    if not all(isinstance(key, str) and isinstance(path, str) for key, path in expose.items()):
        raise ConfigError(f"{field}.response.expose must map output names to paths")
    normalized_request = {
        "method": method.upper(),
        "path": path,
        "headers": _mapping(request.get("headers", {}), f"{field}.request.headers"),
    }
    for key in ("query", "body"):
        if key in request:
            normalized_request[key] = request[key]
    timeout = request.get("timeout_seconds", 30)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ConfigError(f"{field}.request.timeout_seconds must be between 0 and 300")
    normalized_request["timeout_seconds"] = timeout
    policy = _mapping(item.get("policy", {}), f"{field}.policy")
    side_effect = policy.get(
        "side_effect", "read" if method.upper() in {"GET", "HEAD"} else "execute"
    )
    approval = policy.get("approval", "none" if side_effect == "read" else "inherit")
    idempotency = policy.get(
        "idempotency",
        "supported" if method.upper() in {"GET", "HEAD", "PUT", "DELETE"} else "none",
    )
    if side_effect not in SIDE_EFFECTS:
        raise ConfigError(f"{field}.policy.side_effect is unsupported")
    if approval not in APPROVAL_POLICIES:
        raise ConfigError(f"{field}.policy.approval is unsupported")
    if idempotency not in IDEMPOTENCY_POLICIES:
        raise ConfigError(f"{field}.policy.idempotency is unsupported")
    return {
        "description": str(item.get("description", "")).strip(),
        "input": _schema(item.get("input"), f"{field}.input"),
        "request": normalized_request,
        "response": {"expose": expose},
        "policy": {
            "side_effect": side_effect,
            "approval": approval,
            "idempotency": idempotency,
        },
    }


def _integrations(spec: dict[str, Any], connections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw = spec.get("integrations", {})
    values = _mapping(raw, "spec.integrations")
    result: dict[str, Any] = {}
    for name, raw_value in values.items():
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ConfigError(f"invalid integration name: {name}")
        field = f"spec.integrations.{name}"
        item = _mapping(raw_value, field)
        connection = item.get("connection")
        if connection not in connections or connections[connection].get("provider") != "http":
            raise ConfigError(f"{field}.connection must reference an HTTP connection")
        access = _mapping(item.get("access", {"mode": "schema"}), f"{field}.access")
        if set(access) - {
            "mode",
            "source",
            "operations",
            "methods",
            "expose",
            "max_requests",
            "opaque_identifiers",
        }:
            raise ConfigError(f"{field}.access contains unsupported fields")
        mode = access.get("mode", "schema")
        if mode not in {"schema", "openapi", "full"}:
            raise ConfigError(f"{field}.access.mode is unsupported")
        normalized_access: dict[str, Any] = {"mode": mode}
        if "max_requests" in access:
            limit = access["max_requests"]
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ConfigError(f"{field}.access.max_requests must be an integer from 1 to 1000")
            normalized_access["max_requests"] = limit
        if "opaque_identifiers" in access:
            if not isinstance(access["opaque_identifiers"], bool):
                raise ConfigError(f"{field}.access.opaque_identifiers must be true or false")
            normalized_access["opaque_identifiers"] = access["opaque_identifiers"]
        reviewer = item.get("policy")
        if reviewer is not None:
            reviewer = _mapping(reviewer, f"{field}.policy")
            if set(reviewer) - {"instructions", "runner", "model"}:
                raise ConfigError(f"{field}.policy contains unsupported fields")
            if (
                not isinstance(reviewer.get("instructions"), str)
                or not reviewer["instructions"].strip()
            ):
                raise ConfigError(f"{field}.policy.instructions is required")
            _agent_policy(reviewer, f"{field}.policy")
        if mode == "openapi":
            source = access.get("source")
            allow = access.get("operations", [])
            if not isinstance(source, str) or not source.startswith(("https://", "http://")):
                raise ConfigError(f"{field}.access.source must be an HTTP URL")
            if not isinstance(allow, list) or not all(isinstance(value, str) for value in allow):
                raise ConfigError(f"{field}.access.operations must be a string list")
            normalized_access.update({"source": source, "operations": sorted(set(allow))})
        if mode == "full":
            methods = access.get("methods", sorted(HTTP_METHODS))
            if not isinstance(methods, list) or not methods:
                raise ConfigError(f"{field}.access.methods must be a non-empty list")
            methods = [str(method).upper() for method in methods]
            if any(method not in HTTP_METHODS for method in methods):
                raise ConfigError(f"{field}.access.methods contains an unsupported method")
            normalized_access["methods"] = sorted(set(methods))
            expose = access.get("expose", {"result": "body"})
            expose = _mapping(expose, f"{field}.access.expose")
            if not all(
                isinstance(key, str) and isinstance(path, str) for key, path in expose.items()
            ):
                raise ConfigError(f"{field}.access.expose must map output names to paths")
            normalized_access["expose"] = expose
        operations = {
            operation_name: _operation(operation, f"{field}.operations.{operation_name}")
            for operation_name, operation in _mapping(
                item.get("operations", {}), f"{field}.operations"
            ).items()
        }
        if mode == "schema" and not operations:
            raise ConfigError(f"{field}.operations must define at least one operation")
        result[name] = {
            "connection": connection,
            "access": normalized_access,
            "operations": operations,
            **({"policy": reviewer} if reviewer is not None else {}),
        }
    return result


def _load_integration_packages(path: Path, spec: dict[str, Any]) -> None:
    packages = spec.get("integration_packages", [])
    if not isinstance(packages, list):
        raise ConfigError("spec.integration_packages must be a list")
    connections = {
        item["ref"]: {key: value for key, value in item.items() if key != "ref"}
        for item in _named_items(spec.get("connections", {}), "spec.connections")
    }
    integrations = dict(_mapping(spec.get("integrations", {}), "spec.integrations"))
    normalized: list[dict[str, str]] = []
    for index, value in enumerate(packages):
        field = f"spec.integration_packages[{index}]"
        item = _mapping(value, field)
        package_path = _relative_path(path.parent, item.get("path"), f"{field}.path")
        try:
            content = package_path.read_text(encoding="utf-8")
            package = _mapping(yaml.safe_load(content), field)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"could not read integration package {package_path}: {exc}") from exc
        if package.get("apiVersion") != "outcomeci.dev/v1alpha1" or package.get("kind") != (
            "OutcomeIntegrationPackage"
        ):
            raise ConfigError(f"{field} must contain an OutcomeIntegrationPackage")
        metadata = _mapping(package.get("metadata"), f"{field}.metadata")
        name, version = metadata.get("name"), metadata.get("version")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ConfigError(f"{field}.metadata.name is invalid")
        _non_empty_str(version, f"{field}.metadata.version is required")
        package_spec = _mapping(package.get("spec"), f"{field}.spec")
        for connection in _named_items(package_spec.get("connections", {}), f"{field}.connections"):
            ref = connection.pop("ref")
            if ref in connections:
                raise ConfigError(f"integration package connection conflicts with {ref}")
            connections[ref] = connection
        for integration, definition in _mapping(
            package_spec.get("integrations", {}), f"{field}.integrations"
        ).items():
            if integration in integrations:
                raise ConfigError(f"integration package conflicts with {integration}")
            integrations[integration] = definition
        normalized.append(
            {
                "name": name,
                "version": version.strip(),
                "path": package_path.relative_to(path.parent.resolve()).as_posix(),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        )
    spec["connections"] = connections
    spec["integrations"] = integrations
    spec["integration_packages"] = sorted(normalized, key=lambda item: item["name"])


def _validate_custom_connection(item: dict[str, Any], index: int) -> None:
    """Validate a `provider: custom` connection's transport, operations,
    auth, and optional request/response contract schemas. Read-only: unlike
    the `http` provider branch beside this call site, nothing here writes
    back into `item`."""
    transport = _mapping(item.get("transport"), f"spec.connections[{index}].transport")
    transport_type = transport.get("type")
    if transport_type == "http":
        if not isinstance(transport.get("endpoint"), str) or not transport["endpoint"].startswith(
            ("http://", "https://")
        ):
            raise ConfigError(f"spec.connections[{index}].transport.endpoint must be an HTTP URL")
    elif transport_type == "mcp":
        protocol = transport.get("protocol")
        if protocol == "streamable_http" and (
            not isinstance(transport.get("endpoint"), str)
            or not transport["endpoint"].startswith(("http://", "https://"))
        ):
            raise ConfigError(f"spec.connections[{index}].transport.endpoint must be an HTTP URL")
        if protocol == "stdio" and (
            not isinstance(transport.get("command"), list)
            or not transport["command"]
            or not all(isinstance(part, str) for part in transport["command"])
        ):
            raise ConfigError(
                f"spec.connections[{index}].transport.command must be a non-empty string list"
            )
        if protocol not in {"streamable_http", "stdio"}:
            raise ConfigError(f"spec.connections[{index}].transport.protocol is unsupported")
    else:
        raise ConfigError(f"spec.connections[{index}].transport.type is unsupported")
    operations = _mapping(item.get("operations"), f"spec.connections[{index}].operations")
    for operation in ("request", "poll"):
        operation_value = _mapping(
            operations.get(operation),
            f"spec.connections[{index}].operations.{operation}",
        )
        if transport_type == "http" and not isinstance(operation_value.get("path"), str):
            raise ConfigError(f"spec.connections[{index}].operations.{operation}.path is required")
        if transport_type == "mcp" and not isinstance(operation_value.get("tool"), str):
            raise ConfigError(f"spec.connections[{index}].operations.{operation}.tool is required")
    auth = item.get("auth", {})
    if auth:
        auth = _mapping(auth, f"spec.connections[{index}].auth")
        if set(auth) - {"env", "header", "scheme"} or not isinstance(auth.get("env"), str):
            raise ConfigError(
                f"spec.connections[{index}].auth must reference an environment variable"
            )
    contract = item.get("contract", {})
    if contract:
        contract = _mapping(contract, f"spec.connections[{index}].contract")
        if set(contract) - {"request", "poll"}:
            raise ConfigError(f"spec.connections[{index}].contract supports only request and poll")
        for operation, operation_contract in contract.items():
            operation_contract = _mapping(
                operation_contract,
                f"spec.connections[{index}].contract.{operation}",
            )
            if set(operation_contract) - {"input", "output"}:
                raise ConfigError(
                    f"spec.connections[{index}].contract.{operation} supports only input and output"
                )
            for direction, schema in operation_contract.items():
                if not isinstance(schema, dict):
                    raise ConfigError(
                        f"spec.connections[{index}].contract.{operation}.{direction} must be an inline JSON Schema"
                    )
                try:
                    jsonschema.validators.validator_for(schema).check_schema(schema)
                except jsonschema.SchemaError as exc:
                    raise ConfigError(
                        f"spec.connections[{index}].contract.{operation}.{direction} is not a valid JSON Schema: {exc.message}"
                    ) from exc


def _validate_phase_graph(
    normalized_phases: dict[str, Any],
    phases: dict[str, Any],
    triggers: dict[str, Any],
    outputs: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Cross-phase validation once every phase's own contract is already
    normalized: dependency edges point at real, non-self phases; every
    input's `from` resolves to a declared producer that's a direct
    dependency (or a known trigger/runtime/context source); the same for
    reaction and reply hooks' output sources."""
    for phase_name, phase in normalized_phases.items():
        for dependency in phase["needs"]:
            if dependency == phase_name:
                raise ConfigError(f"phase {phase_name} cannot depend on itself")
            if dependency not in phases:
                raise ConfigError(f"phase {phase_name} needs unknown phase {dependency}")
        for item in phase["inputs"]:
            source = item["from"]
            if source.startswith(("runtime.", "context.")):
                continue
            if source.startswith("trigger."):
                trigger_name = source.removeprefix("trigger.")
                if trigger_name not in triggers:
                    raise ConfigError(
                        f"input {phase_name}.{item['name']} references unknown trigger {trigger_name}"
                    )
                continue
            match = re.fullmatch(
                r"([a-z][a-z0-9_-]{0,62})\.outputs\.([a-z][a-z0-9_-]{0,62})", source
            )
            if not match or (match.group(1), match.group(2)) not in outputs:
                raise ConfigError(
                    f"input {phase_name}.{item['name']} has no declared producer: {source}"
                )
            if match.group(1) not in phase["needs"]:
                raise ConfigError(
                    f"input {phase_name}.{item['name']} must come from a direct dependency"
                )
        for timing_group in phase["humans"].values():
            for hook in timing_group:
                if hook["delivery"].get("type") != "slack" or hook["delivery"].get("mode") not in {
                    "reaction",
                    "reply",
                }:
                    continue
                source = hook["delivery"]["source"]
                match = re.fullmatch(
                    r"([a-z][a-z0-9_-]{0,62})\.outputs\.([a-z][a-z0-9_-]{0,62})", source
                )
                if not match or (match.group(1), match.group(2)) not in outputs:
                    raise ConfigError(
                        f"phase {phase_name} human hook {hook['id']} references unknown output: {source}"
                    )
                if match.group(1) not in phase["needs"]:
                    raise ConfigError(
                        f"phase {phase_name} human hook {hook['id']} must reference a direct dependency's output"
                    )


def _load_v1alpha1(path: Path) -> dict[str, Any]:
    try:
        root = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "document")
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if root.get("apiVersion") != "outcomeci.dev/v1alpha1":
        raise ConfigError("apiVersion must be outcomeci.dev/v1alpha1")
    if root.get("kind") != "OutcomeWorkflow":
        raise ConfigError("kind must be OutcomeWorkflow")
    metadata = _mapping(root.get("metadata"), "metadata")
    _non_empty_str(metadata.get("name"), "metadata.name is required")
    spec = _mapping(root.get("spec"), "spec")
    _load_integration_packages(path.resolve(), spec)
    triggers = _triggers(spec.get("triggers"))
    spec["triggers"] = triggers
    for field, choices in (
        ("backend", {"outcomeci", "filesystem"}),
        ("context", {"outcomeci", "http", "filesystem"}),
    ):
        value = _mapping(spec.get(field, {"provider": "outcomeci"}), f"spec.{field}")
        if value.get("provider", "outcomeci") not in choices:
            raise ConfigError(f"unsupported spec.{field}.provider")
        if field == "context":
            for patterns_name in ("include", "exclude"):
                patterns = value.get(patterns_name, [])
                if not isinstance(patterns, list) or not all(
                    isinstance(pattern, str) and pattern.strip() for pattern in patterns
                ):
                    raise ConfigError(
                        f"spec.context.{patterns_name} must be a list of non-empty glob strings"
                    )

    agents = _mapping(spec.get("agents", {}), "spec.agents")
    typed = "orchestrator" in agents
    if typed:
        if "instructions" in spec:
            raise ConfigError("use only spec.agents.orchestrator, not both orchestrator forms")
        configured = _mapping(agents["orchestrator"], "spec.agents.orchestrator")
        if set(configured) - {"instructions", "runner", "model"}:
            raise ConfigError("spec.agents.orchestrator contains unsupported fields")
        if (
            not isinstance(configured.get("instructions"), str)
            or not configured["instructions"].strip()
        ):
            raise ConfigError("spec.agents.orchestrator.instructions is required")
        instructions = {
            "orchestrator": {
                "path": configured["instructions"],
                **_agent_policy(configured, "spec.agents.orchestrator"),
            }
        }
    else:
        instructions = _mapping(spec.get("instructions"), "spec.instructions")
    if len(instructions) != 1:
        raise ConfigError("spec.instructions must define exactly one orchestrator")
    orchestrator_name, orchestrator_value = next(iter(instructions.items()))
    if not IDENTIFIER.fullmatch(str(orchestrator_name)):
        raise ConfigError("the orchestrator name must be a valid identifier")
    if isinstance(orchestrator_value, str):
        orchestrator_value = {"path": orchestrator_value}
        instructions[orchestrator_name] = orchestrator_value
    orchestrator = _mapping(orchestrator_value, f"spec.instructions.{orchestrator_name}")
    if not isinstance(orchestrator.get("path"), str):
        raise ConfigError(f"spec.instructions.{orchestrator_name}.path is required")
    _agent_policy(orchestrator, f"spec.instructions.{orchestrator_name}")

    default_raw = _mapping(agents.get("default", {}), "spec.agents.default")
    unknown_default = set(default_raw) - {"runner", "model", "fallback"}
    if unknown_default:
        raise ConfigError(
            f"spec.agents.default has unknown fields: {', '.join(sorted(unknown_default))}"
        )
    default = _agent_policy(default_raw, "spec.agents.default")
    if "fallback" in default_raw:
        fallback = _agent_policy(default_raw["fallback"], "spec.agents.default.fallback")
        if not fallback.get("runner"):
            raise ConfigError("spec.agents.default.fallback.runner is required")
        if fallback["runner"] == default.get("runner", "codex"):
            raise ConfigError(
                "spec.agents.default.fallback.runner must differ from the default runner"
            )
        default["fallback"] = fallback
    phases = _mapping(agents.get("phases", {}), "spec.agents.phases")
    if not phases:
        raise ConfigError("spec.agents.phases must define at least one phase")
    outputs: dict[tuple[str, str], dict[str, Any]] = {}
    output_paths: set[str] = set()
    normalized_phases: dict[str, Any] = {}
    for phase_name, raw_policy in phases.items():
        if not isinstance(phase_name, str) or not IDENTIFIER.fullmatch(phase_name):
            raise ConfigError(f"invalid outcome phase: {phase_name}")
        field = f"spec.agents.phases.{phase_name}"
        policy = _mapping(raw_policy, field)
        if typed or "type" in policy:
            try:
                validate_contract("agent", policy)
            except ContractError as exc:
                raise ConfigError(f"{field}: {exc}") from exc
        with_values = _mapping(policy.get("with", {}), f"{field}.with")
        if not isinstance(policy.get("instructions"), str):
            raise ConfigError(f"{field}.instructions is required")
        _agent_policy(policy, field)
        needs = policy.get("needs", [])
        if (
            not isinstance(needs, list)
            or not all(isinstance(item, str) for item in needs)
            or len(needs) != len(set(needs))
        ):
            raise ConfigError(f"{field}.needs must be a list of unique phase names")
        expects = _mapping(policy.get("expects", {}), f"{field}.expects")
        raw_inputs, raw_outputs = expects.get("inputs", []), expects.get("outputs", [])
        if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
            raise ConfigError(f"{field}.expects inputs and outputs must be lists")
        inputs = [
            _contract(item, f"{field}.expects.inputs[{index}]", output=False)
            for index, item in enumerate(raw_inputs)
        ]
        phase_outputs = [
            _contract(item, f"{field}.expects.outputs[{index}]", output=True)
            for index, item in enumerate(raw_outputs)
        ]
        if len({item["name"] for item in inputs}) != len(inputs) or len(
            {item["name"] for item in phase_outputs}
        ) != len(phase_outputs):
            raise ConfigError(f"{field} contract names must be unique")
        for item in phase_outputs:
            if item["path"] in output_paths:
                raise ConfigError(f"duplicate output path: {item['path']}")
            output_paths.add(item["path"])
            outputs[(phase_name, item["name"])] = item
        capabilities, required_capabilities, humans = _phase_integrations(policy, field)
        normalized_phases[phase_name] = {
            "type": "agent",
            "with": with_values,
            "needs": needs,
            "inputs": inputs,
            "outputs": phase_outputs,
            "humans": humans,
            "capabilities": capabilities,
            "required_capabilities": required_capabilities,
        }

    _validate_phase_graph(normalized_phases, phases, triggers, outputs)

    indegree = {name: len(value["needs"]) for name, value in normalized_phases.items()}
    remaining = set(normalized_phases)
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(name for name in remaining if indegree[name] == 0)
        if not ready:
            raise ConfigError("outcome phase graph contains a cycle")
        levels.append(ready)
        remaining.difference_update(ready)
        for name in remaining:
            indegree[name] -= sum(
                dependency in ready for dependency in normalized_phases[name]["needs"]
            )

    connections = _named_items(spec.get("connections", []), "spec.connections")
    spec["connections"] = connections
    refs: set[str] = set()
    connection_providers: dict[str, str] = {}
    for index, value in enumerate(connections):
        item = _mapping(value, f"spec.connections[{index}]")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref or ref in refs:
            raise ConfigError("connection references must be unique non-empty strings")
        refs.add(ref)
        provider = item.get("provider")
        if provider not in {"slack", "custom", "http"}:
            raise ConfigError(f"spec.connections[{index}].provider is unsupported")
        connection_providers[ref] = provider
        if provider == "http":
            item["base_url"] = _http_origin(
                item.get("base_url"), f"spec.connections[{index}].base_url"
            )
            item["auth"] = _http_auth(item.get("auth"), f"spec.connections[{index}].auth")
            allow_private = item.get("allow_private_network", False)
            if not isinstance(allow_private, bool):
                raise ConfigError(
                    f"spec.connections[{index}].allow_private_network must be boolean"
                )
            item["allow_private_network"] = allow_private
        if provider == "custom":
            _validate_custom_connection(item, index)
    normalized_integrations = _integrations(spec, {item["ref"]: item for item in connections})
    spec["integrations"] = normalized_integrations
    available_capabilities = (
        {
            f"{integration}.{operation}"
            for integration, value in normalized_integrations.items()
            for operation in value["operations"]
        }
        | {
            f"{integration}.request"
            for integration, value in normalized_integrations.items()
            if value["access"]["mode"] == "full"
        }
        | {
            f"{integration}.{re.sub(r'[^a-z0-9_-]+', '_', operation.lower()).strip('_')}"
            for integration, value in normalized_integrations.items()
            if value["access"]["mode"] == "openapi"
            for operation in value["access"]["operations"]
        }
    )
    for phase_name, phase in normalized_phases.items():
        unknown_capabilities = set(phase["capabilities"]) - available_capabilities
        if unknown_capabilities:
            raise ConfigError(
                f"phase {phase_name} references unknown capabilities: {', '.join(sorted(unknown_capabilities))}"
            )
        for timing in ("before", "during", "after"):
            for hook in phase["humans"][timing]:
                delivery = hook["delivery"]
                if delivery.get("type") in {"slack", "custom"} and not (
                    delivery.get("type") == "slack"
                    and delivery.get("mode") in {"reaction", "reply"}
                ):
                    ref = delivery["connection"]
                    if connection_providers.get(ref) != delivery["type"]:
                        raise ConfigError(
                            f"human hook {phase_name}.{hook['id']} references an incompatible connection"
                        )
    root["_graph"] = {
        "orchestrator": orchestrator_name,
        "orchestrator_config": orchestrator,
        "levels": levels,
        "phases": normalized_phases,
        "default_policy": default,
        "triggers": triggers,
    }
    return root


# API versions are durable behavior contracts, not aliases for the newest
# package implementation. Never replace an entry with incompatible rules; add a
# new API version and keep the older compiler available.
COMPILER_REGISTRY = {"outcomeci.dev/v1alpha1": _load_v1alpha1}


def load(path: Path) -> dict[str, Any]:
    try:
        document = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "document")
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    api_version = document.get("apiVersion")
    compiler = COMPILER_REGISTRY.get(api_version)
    if compiler is None:
        supported = ", ".join(sorted(COMPILER_REGISTRY))
        raise ConfigError(
            f"unsupported apiVersion {api_version!r}; supported versions: {supported}"
        )
    return compiler(path)


def _excluded(relative: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern)
        or (
            pattern.endswith("/**")
            and (relative == pattern[:-3] or relative.startswith(pattern[:-2]))
        )
        for pattern in patterns
    )


def _filesystem_context(root: Path, context: dict[str, Any]) -> list[dict[str, Any]]:
    paths: dict[str, Path] = {}
    for pattern in context.get("include", []):
        matches = (
            (root / pattern[:-3]).rglob("*")
            if pattern.endswith("/**") and (root / pattern[:-3]).is_dir()
            else root.glob(pattern)
        )
        for path in matches:
            if path.is_file():
                relative = path.resolve().relative_to(root.resolve()).as_posix()
                if not private_path(relative) and not _excluded(
                    relative, context.get("exclude", [])
                ):
                    paths[relative] = path.resolve()
    if len(paths) > 5000:
        raise ConfigError("filesystem context exceeds 5000 files")
    return [
        {
            "path": relative,
            "byte_size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for relative, path in sorted(paths.items())
    ]


def compile_workflow(path: Path) -> dict[str, Any]:
    document = load(path)
    graph = document.pop("_graph")
    spec, root = document["spec"], path.parent
    orchestrator_name = graph["orchestrator"]
    orchestrator_config = graph["orchestrator_config"]
    orchestrator = _reference(
        root, orchestrator_config["path"], f"spec.instructions.{orchestrator_name}.path"
    )
    default = graph["default_policy"]
    orchestrator["name"] = orchestrator_name
    orchestrator["policy"] = {
        "runner": orchestrator_config.get("runner", default.get("runner")),
        "model": orchestrator_config.get("model", default.get("model")),
    }
    phases: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    for phase_name, contract in graph["phases"].items():
        policy = spec["agents"]["phases"][phase_name]
        phases[phase_name] = {
            **_reference(
                root,
                policy["instructions"],
                f"spec.agents.phases.{phase_name}.instructions",
            ),
            "needs": contract["needs"],
            "type": contract["type"],
            "with": contract["with"],
            "expects": {"inputs": contract["inputs"], "outputs": contract["outputs"]},
            "humans": contract["humans"],
            "capabilities": contract["capabilities"],
            "required_capabilities": contract["required_capabilities"],
            "policy": {
                "runner": policy.get("runner", default.get("runner")),
                "model": policy.get("model", default.get("model")),
            },
        }
        for direction in ("inputs", "outputs"):
            for item in contract[direction]:
                if direction == "inputs" and item["from"].startswith("trigger."):
                    trigger_type = graph["triggers"][item["from"].removeprefix("trigger.")]["type"]
                    if trigger_type != "manual":
                        inherited = contract_schema(trigger_type)
                        if "schema" in item:
                            raise ConfigError(
                                f"{phase_name}.{item['name']} inherits its trigger schema; do not override it"
                            )
                        item["schema"] = inherited
                if isinstance(item.get("schema"), (dict, bool)):
                    value = item["schema"]
                    content = json.dumps(value, sort_keys=True, separators=(",", ":"))
                    key = f"inline:{phase_name}:{direction}:{item['name']}"
                    schemas[key] = {
                        "content": content,
                        "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "value": value,
                    }
                    item["schema"] = key
                elif item.get("schema") and item["schema"] not in schemas:
                    schemas[item["schema"]] = _reference(
                        root, item["schema"], "artifact schema", json_value=True
                    )
    normalized = json.loads(json.dumps(document, sort_keys=True, separators=(",", ":")))
    context = spec.get("context", {"provider": "outcomeci"})
    context_files = (
        _filesystem_context(root, context) if context.get("provider") == "filesystem" else []
    )
    reviewers = {}
    for name, integration in spec.get("integrations", {}).items():
        if integration.get("policy"):
            reviewer = integration["policy"]
            reviewers[name] = {
                **_reference(
                    root,
                    reviewer["instructions"],
                    f"spec.integrations.{name}.policy.instructions",
                ),
                "policy": {
                    "runner": reviewer.get("runner", default.get("runner")),
                    "model": reviewer.get("model", default.get("model")),
                },
            }
    resolved = {
        "orchestrator": orchestrator,
        "phases": phases,
        "schemas": schemas,
        "integration_policies": reviewers,
    }
    revision_input = {
        "workflow": normalized,
        "graph": {"levels": graph["levels"]},
        "instructions": resolved,
        "context": {
            "provider": context.get("provider", "outcomeci"),
            "files": context_files,
        },
        "triggers": graph["triggers"],
    }
    revision = hashlib.sha256(
        json.dumps(revision_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "outcomeci.workflow/v1alpha1",
        "api_version": document["apiVersion"],
        "engine_version": "2",
        "engine_package_version": __version__,
        "workflow_revision": revision,
        **revision_input,
    }
