"""Validation and deterministic compilation of outcomeci.workflow/v1 files."""

from __future__ import annotations

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
from .security import private_path

RUNNERS = {"codex", "claude", "opencode"}
AUTH_KINDS = {
    "none",
    "token",
    "api_key",
    "basic",
    "oauth2",
    "oidc",
    "jwt_bearer",
    "app_installation",
}
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


def _contract(value: Any, field: str) -> dict[str, Any]:
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
    path = item.get("path")
    if (
        not isinstance(path, str)
        or not path.strip()
        or Path(path).is_absolute()
        or ".." in Path(path).parts
    ):
        raise ConfigError(f"{field}.path must remain within the run directory")
    result["path"] = Path(path).as_posix()
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
    """A connection's auth: the Vault credential it names and the kinds its
    connector accepts. The runtime picks the accepted kind that matches the
    credential; a connector that takes no credential accepts only `none`."""
    auth = _mapping(value, field)
    connector = _non_empty_str(auth.get("connector"), f"{field}.connector is required")
    accepts = auth.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise ConfigError(f"{field}.accepts must be a non-empty list")
    kinds = []
    for index, entry in enumerate(accepts):
        entry = _mapping(entry, f"{field}.accepts[{index}]")
        if entry.get("kind") not in AUTH_KINDS:
            raise ConfigError(f"{field}.accepts[{index}].kind is unsupported")
        kinds.append(entry["kind"])
    if len(set(kinds)) != len(kinds) or ("none" in kinds and len(kinds) > 1):
        raise ConfigError(f"{field}.accepts lists a kind twice or mixes none with others")
    result: dict[str, Any] = {"connector": connector, "accepts": accepts}
    if kinds == ["none"]:
        if auth.get("credential") is not None:
            raise ConfigError(f"{field}.credential is not used: {connector} takes no credential")
        return result
    result["credential"] = _non_empty_str(
        auth.get("credential"), f"{field}.credential is required"
    ).strip()
    return result


def _request_operation(item: dict[str, Any], request: dict[str, Any], field: str) -> dict[str, Any]:
    """An operation whose method and path the caller chooses, within a method set."""
    methods = request.get("methods")
    if not isinstance(methods, list) or not methods:
        raise ConfigError(f"{field}.request.methods must be a non-empty list")
    methods = sorted({str(method).upper() for method in methods})
    if any(method not in HTTP_METHODS for method in methods):
        raise ConfigError(f"{field}.request.methods contains an unsupported method")
    reads_only = set(methods) <= {"GET", "HEAD"}
    policy = _mapping(item.get("policy", {}), f"{field}.policy")
    side_effect = policy.get("side_effect", "read" if reads_only else "execute")
    if side_effect not in SIDE_EFFECTS:
        raise ConfigError(f"{field}.policy.side_effect is unsupported")
    expose = _mapping(
        _mapping(item.get("response", {}), f"{field}.response").get("expose", {"result": "body"}),
        f"{field}.response.expose",
    )
    return {
        "description": str(item.get("description", "")).strip(),
        "input": {
            "type": "object",
            "required": ["method", "path"],
            "properties": {
                "method": {"enum": methods},
                "path": {"type": "string", "pattern": "^/[^/].*|^/$"},
                "query": {"type": "object"},
                "headers": {"type": "object"},
                "body": {},
                "purpose": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
        "request": {"methods": methods},
        "response": {"expose": expose},
        "policy": {
            "side_effect": side_effect,
            "approval": "none" if side_effect == "read" else "inherit",
            "idempotency": "none",
        },
        **_grantable(item, field),
        **_compare(item, field),
    }


def _compare(item: dict[str, Any], field: str) -> dict[str, Any]:
    """Which writes replace a file, reviewed as a diff against its current copy."""
    if not item.get("compare"):
        return {}
    compare = item["compare"]
    if not isinstance(compare, list):
        raise ConfigError(f"{field}.compare must be a list")
    for index, rule in enumerate(compare):
        rule = _mapping(rule, f"{field}.compare[{index}]")
        paths = [rule.get("proposed"), rule.get("current")]
        if rule.get("ref") is not None:
            paths.append(rule["ref"])
        if (
            not all(isinstance(path, str) and path.startswith("body") for path in paths)
            or rule.get("encoding") not in {"base64", "text"}
            or not isinstance(rule.get("methods"), list)
        ):
            raise ConfigError(
                f"{field}.compare[{index}] needs methods, proposed, current and optional ref "
                "body paths, and a base64 or text encoding"
            )
        try:
            re.compile(str(rule.get("path")))
        except re.error as exc:
            raise ConfigError(f"{field}.compare[{index}].path is not a pattern") from exc
    return {"compare": compare}


def _grantable(item: dict[str, Any], field: str) -> dict[str, Any]:
    """Which grant arguments may scope this operation, and which requests it
    refuses outright, as a connector declares them."""
    result: dict[str, Any] = {}
    if "grantable" in item:
        grantable = _mapping(item["grantable"], f"{field}.grantable")
        for name, rule in grantable.items():
            rule = _mapping(rule, f"{field}.grantable.{name}")
            if sum(kind in rule for kind in ("field", "path_prefix", "response_in")) != 1:
                raise ConfigError(
                    f"{field}.grantable.{name} sets exactly one of field, path_prefix "
                    "or response_in"
                )
            paths = rule.get("response_in", ["body"])
            if (
                not isinstance(paths, list)
                or not paths
                or not all(isinstance(path, str) and path.startswith("body") for path in paths)
            ):
                raise ConfigError(f"{field}.grantable.{name}.response_in must list body paths")
        result["grantable"] = grantable
    if item.get("deny"):
        deny = item["deny"]
        if not isinstance(deny, list):
            raise ConfigError(f"{field}.deny must be a list")
        for index, rule in enumerate(deny):
            rule = _mapping(rule, f"{field}.deny[{index}]")
            try:
                re.compile(str(rule.get("path")))
            except re.error as exc:
                raise ConfigError(f"{field}.deny[{index}].path is not a pattern") from exc
        result["deny"] = deny
    return result


def _download(response: dict[str, Any], field: str) -> dict[str, Any]:
    """A connector's download: an https host list, response paths and a limit."""
    if "download" not in response:
        return {}
    download = _mapping(response["download"], f"{field}.response.download")
    hosts = download.get("hosts")
    if (
        set(download) != {"url", "hosts", "name", "content_type", "max_bytes"}
        or not all(
            isinstance(download[key], str) and download[key].startswith("body")
            for key in ("url", "name", "content_type")
        )
        or not isinstance(hosts, list)
        or not hosts
        or not all(isinstance(host, str) and host for host in hosts)
        or not isinstance(download["max_bytes"], int)
        or not 0 < download["max_bytes"] <= 100 * 1024 * 1024
    ):
        raise ConfigError(
            f"{field}.response.download needs url, name and content_type paths, hosts "
            "and max_bytes up to 100 MiB"
        )
    return {"download": dict(download)}


def _operation(value: Any, field: str) -> dict[str, Any]:
    item = _mapping(value, field)
    request = _mapping(item.get("request"), f"{field}.request")
    if "methods" in request:
        return _request_operation(item, request, field)
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
        "response": {"expose": expose, **_download(response, field)},
        "policy": {
            "side_effect": side_effect,
            "approval": approval,
            "idempotency": idempotency,
        },
        **_grantable(item, field),
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
        if set(access) - {"mode", "max_requests"}:
            raise ConfigError(f"{field}.access contains unsupported fields")
        mode = access.get("mode", "schema")
        if mode != "schema":
            raise ConfigError(f"{field}.access.mode is unsupported")
        normalized_access: dict[str, Any] = {"mode": mode}
        if "max_requests" in access:
            limit = access["max_requests"]
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
                raise ConfigError(f"{field}.access.max_requests must be an integer from 1 to 1000")
            normalized_access["max_requests"] = limit
        operations = {
            operation_name: _operation(operation, f"{field}.operations.{operation_name}")
            for operation_name, operation in _mapping(
                item.get("operations", {}), f"{field}.operations"
            ).items()
        }
        if not operations:
            raise ConfigError(f"{field}.operations must define at least one operation")
        result[name] = {
            "connection": connection,
            "access": normalized_access,
            "operations": operations,
        }
    return result


def _inline_content(value: Any) -> bool:
    """Instructions given as {content: <text>} instead of a file path."""
    return (
        isinstance(value, dict)
        and set(value) <= {"content", "runner", "model"}
        and isinstance(value.get("content"), str)
        and bool(value["content"].strip())
    )


# Every phase carries these keys so a compiled workflow has one shape; a
# lowered v1 step fills only its needs, outputs and capabilities.
PHASE_DEFAULTS = {
    "type": "agent",
    "with": {},
    "inputs": [],
    "humans": {"before": [], "during": [], "after": []},
    "required_capabilities": [],
}


def validate_lowered(root: dict[str, Any], path: Path) -> dict[str, Any]:
    """Validate a lowered workflow document and attach its phase graph.

    `v1.lower` turns an outcomeci.workflow/v1 file into this shape: one phase
    per step, HTTP connections and schema integrations for its APIs, and
    inline orchestrator and step instructions. Every run executes it."""
    if root.get("kind") != "OutcomeWorkflow":
        raise ConfigError("kind must be OutcomeWorkflow")
    metadata = _mapping(root.get("metadata"), "metadata")
    _non_empty_str(metadata.get("name"), "metadata.name is required")
    spec = _mapping(root.get("spec"), "spec")
    spec["integrations"] = dict(_mapping(spec.get("integrations", {}), "spec.integrations"))
    spec["integration_packages"] = []
    triggers = _triggers(spec.get("triggers"))
    spec["triggers"] = triggers
    for field in ("backend", "context"):
        value = _mapping(spec.get(field, {"provider": "outcomeci"}), f"spec.{field}")
        if value.get("provider", "outcomeci") != "outcomeci":
            raise ConfigError(f"unsupported spec.{field}.provider")

    agents = _mapping(spec.get("agents", {}), "spec.agents")
    instructions = _mapping(spec.get("instructions"), "spec.instructions")
    if len(instructions) != 1:
        raise ConfigError("spec.instructions must define exactly one orchestrator")
    orchestrator_name, orchestrator_value = next(iter(instructions.items()))
    if not IDENTIFIER.fullmatch(str(orchestrator_name)):
        raise ConfigError("the orchestrator name must be a valid identifier")
    orchestrator = _mapping(orchestrator_value, f"spec.instructions.{orchestrator_name}")
    if not isinstance(orchestrator.get("path"), str) and not _inline_content(orchestrator):
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
    output_paths: set[str] = set()
    normalized_phases: dict[str, Any] = {}
    for phase_name, raw_policy in phases.items():
        if not isinstance(phase_name, str) or not IDENTIFIER.fullmatch(phase_name):
            raise ConfigError(f"invalid outcome phase: {phase_name}")
        field = f"spec.agents.phases.{phase_name}"
        policy = _mapping(raw_policy, field)
        if not isinstance(policy.get("instructions"), str) and not _inline_content(
            policy.get("instructions")
        ):
            raise ConfigError(f"{field}.instructions is required")
        _agent_policy(policy, field)
        needs = policy.get("needs", [])
        if (
            not isinstance(needs, list)
            or not all(isinstance(item, str) for item in needs)
            or len(needs) != len(set(needs))
        ):
            raise ConfigError(f"{field}.needs must be a list of unique phase names")
        for dependency in needs:
            if dependency == phase_name:
                raise ConfigError(f"phase {phase_name} cannot depend on itself")
            if dependency not in phases:
                raise ConfigError(f"phase {phase_name} needs unknown phase {dependency}")
        expects = _mapping(policy.get("expects", {}), f"{field}.expects")
        if expects.get("inputs"):
            raise ConfigError(f"{field}.expects.inputs must be empty")
        raw_outputs = expects.get("outputs", [])
        if not isinstance(raw_outputs, list):
            raise ConfigError(f"{field}.expects.outputs must be a list")
        phase_outputs = [
            _contract(item, f"{field}.expects.outputs[{index}]")
            for index, item in enumerate(raw_outputs)
        ]
        if len({item["name"] for item in phase_outputs}) != len(phase_outputs):
            raise ConfigError(f"{field} contract names must be unique")
        for item in phase_outputs:
            if item["path"] in output_paths:
                raise ConfigError(f"duplicate output path: {item['path']}")
            output_paths.add(item["path"])
        capabilities = policy.get("capabilities", [])
        if not isinstance(capabilities, list) or not all(
            isinstance(capability, str) and capability.strip() for capability in capabilities
        ):
            raise ConfigError(f"{field}.capabilities must be a list of names")
        normalized_phases[phase_name] = {
            **json.loads(json.dumps(PHASE_DEFAULTS)),
            "needs": needs,
            "outputs": phase_outputs,
            "capabilities": sorted(set(capabilities)),
        }

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
    for index, value in enumerate(connections):
        item = _mapping(value, f"spec.connections[{index}]")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref or ref in refs:
            raise ConfigError("connection references must be unique non-empty strings")
        refs.add(ref)
        if item.get("provider") != "http":
            raise ConfigError(f"spec.connections[{index}].provider is unsupported")
        item["base_url"] = _http_origin(item.get("base_url"), f"spec.connections[{index}].base_url")
        item["auth"] = _http_auth(item.get("auth"), f"spec.connections[{index}].auth")
        allow_private = item.get("allow_private_network", False)
        if not isinstance(allow_private, bool):
            raise ConfigError(f"spec.connections[{index}].allow_private_network must be boolean")
        item["allow_private_network"] = allow_private
    normalized_integrations = _integrations(spec, {item["ref"]: item for item in connections})
    spec["integrations"] = normalized_integrations
    available_capabilities = {
        f"{integration}.{operation}"
        for integration, value in normalized_integrations.items()
        for operation in value["operations"]
    }
    for phase_name, phase in normalized_phases.items():
        unknown_capabilities = set(phase["capabilities"]) - available_capabilities
        if unknown_capabilities:
            raise ConfigError(
                f"phase {phase_name} references unknown capabilities: {', '.join(sorted(unknown_capabilities))}"
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


API_VERSION = "outcomeci.workflow/v1"


def load(path: Path) -> dict[str, Any]:
    try:
        document = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "document")
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    api_version = document.get("apiVersion")
    if api_version != API_VERSION:
        raise ConfigError(f"unsupported apiVersion {api_version!r}; use {API_VERSION}")
    from .v1 import load as load_v1

    return load_v1(path)


def _instructions(root: Path, value: Any, field: str) -> dict[str, Any]:
    """Resolve instructions given as a path, {path: ...}, or inline {content: ...}."""
    if isinstance(value, dict) and "content" in value and "path" not in value:
        content = value["content"]
        return {
            "path": None,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
        }
    relative = value["path"] if isinstance(value, dict) else value
    return _reference(root, relative, field)


def compile_workflow(path: Path) -> dict[str, Any]:
    return _compile(load(path), path.parent)


def compile_lowered(document: dict[str, Any], path: Path) -> dict[str, Any]:
    """Compile a document already in the lowered phase-graph shape.

    `path` is where the document would live: instruction and schema files
    resolve beside it."""
    return _compile(validate_lowered(document, path), path.parent)


def _compile(document: dict[str, Any], root: Path) -> dict[str, Any]:
    graph = document.pop("_graph")
    spec = document["spec"]
    orchestrator_name = graph["orchestrator"]
    orchestrator_config = graph["orchestrator_config"]
    orchestrator = _instructions(
        root, orchestrator_config, f"spec.instructions.{orchestrator_name}.path"
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
            **_instructions(
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
            **({"v1": contract["v1"]} if "v1" in contract else {}),
        }
        for item in contract["outputs"]:
            if isinstance(item.get("schema"), (dict, bool)):
                value = item["schema"]
                content = json.dumps(value, sort_keys=True, separators=(",", ":"))
                key = f"inline:{phase_name}:outputs:{item['name']}"
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
    resolved = {
        "orchestrator": orchestrator,
        "phases": phases,
        "schemas": schemas,
        # Integration-level reviewers are gone; the key keeps revisions stable.
        "integration_policies": {},
    }
    revision_input = {
        **({"source": graph["source"]} if "source" in graph else {}),
        **({"connectors": graph["connectors"]} if "connectors" in graph else {}),
        "workflow": normalized,
        "graph": {"levels": graph["levels"]},
        "instructions": resolved,
        "context": {"provider": "outcomeci", "files": []},
        "triggers": graph["triggers"],
    }
    revision = hashlib.sha256(
        json.dumps(revision_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "outcomeci.workflow/v1",
        "api_version": document["apiVersion"],
        "engine_version": "2",
        "engine_package_version": __version__,
        "workflow_revision": revision,
        **revision_input,
    }


def validate_delivery_config(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) - {"type", "delivery", "receiver"} or value.get("delivery", "queued") != "queued":
        raise ConfigError("Webhooks support asynchronous queued delivery only")
    config: dict[str, Any] = {"type": "webhook.received", "delivery": "queued"}
    receiver = value.get("receiver")
    if receiver is not None:
        # A provider that verifies and translates the request before it is
        # queued: its name, the vault reference of its signing secret, and the
        # events that start a run.
        if (
            not isinstance(receiver, dict)
            or set(receiver) != {"uses", "secret", "events"}
            or not isinstance(receiver["uses"], str)
            or not isinstance(receiver["secret"], str)
            or not receiver["secret"].startswith("vault:")
            or not isinstance(receiver["events"], list)
            or not receiver["events"]
            or not all(isinstance(event, str) for event in receiver["events"])
        ):
            raise ConfigError("webhook receiver needs uses, a vault: secret and events")
        config["receiver"] = dict(receiver)
    return config
