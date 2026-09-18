"""Credential-blind execution of workflow-authorized HTTP capabilities."""

from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
import jsonschema
import yaml

from .config import ConfigError, compile_workflow
from .process import ExecutionError

CredentialResolver = Callable[[str], Mapping[str, str] | str]
TEMPLATE = re.compile(r"{{\s*input(?:\.([A-Za-z0-9_.-]+))?\s*}}")


class IntegrationError(ExecutionError):
    """Stable, credential-safe error returned by every integration boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str,
        retryable: bool = False,
    ):
        super().__init__(message, retryable)
        self.code = code
        self.category = category

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "message": str(self),
            "retryable": self.retryable,
        }


def environment_resolver(reference: str) -> Mapping[str, str] | str:
    name = reference.removeprefix("env:")
    if not reference.startswith("env:"):
        raise IntegrationError(
            "integration.credential_reference_invalid",
            "local credentials must use an env: reference",
            category="configuration",
        )
    value = os.environ.get(name)
    if value is None:
        raise IntegrationError(
            "integration.credential_unavailable",
            f"credential environment variable {name} is not set",
            category="authorization",
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        return parsed
    return value


def local_credential_resolver(root: Path) -> CredentialResolver:
    def resolve(reference: str) -> Mapping[str, str] | str:
        if reference.startswith("vault:"):
            from .local_vault import resolve as resolve_local_vault

            return resolve_local_vault(root, reference)
        return environment_resolver(reference)

    return resolve


def _lookup(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, Mapping):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _render(value: Any, inputs: Mapping[str, Any], *, path_value: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _render(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, inputs) for item in value]
    if not isinstance(value, str):
        return value
    match = TEMPLATE.fullmatch(value)
    if match:
        found = _lookup(inputs, match.group(1) or "")
        return quote(str(found), safe="") if path_value else found

    def replace(match: re.Match[str]) -> str:
        found = _lookup(inputs, match.group(1) or "")
        if isinstance(found, (dict, list)):
            raise ExecutionError("structured input must occupy an entire template value")
        return quote(str(found), safe="") if path_value else str(found)

    return TEMPLATE.sub(replace, value)


def _safe_destination(url: str, allow_private: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise IntegrationError(
            "integration.destination_invalid",
            "integration destination is invalid",
            category="policy",
        )
    if allow_private:
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port)}
    except OSError as exc:
        raise IntegrationError(
            "integration.destination_unresolved",
            "integration destination could not be resolved",
            category="transport",
            retryable=True,
        ) from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise IntegrationError(
                "integration.private_destination_denied",
                "integration destination resolves to a non-public address",
                category="policy",
            )


def _credential_mapping(value: Mapping[str, str] | str) -> dict[str, str]:
    if isinstance(value, Mapping) and isinstance(value.get("secrets"), Mapping):
        secrets = value["secrets"]
        configuration = value.get("configuration", {})
        return {
            **configuration,
            **secrets,
            "value": secrets.get("api_key", secrets.get("value", "")),
        }
    return dict(value) if isinstance(value, Mapping) else {"value": value}


def _token(
    client: httpx.Client,
    auth: dict[str, Any],
    credential: dict[str, str],
) -> str:
    token_url = auth.get("token_url")
    if auth["type"] == "oidc":
        discovery = client.get(auth["discovery_url"])
        discovery.raise_for_status()
        token_url = discovery.json().get("token_endpoint")
    if not isinstance(token_url, str):
        raise ExecutionError("authorization server did not provide a token endpoint")
    data = {"grant_type": auth.get("grant_type", "client_credentials")}
    if auth.get("scope"):
        data["scope"] = auth["scope"]
    if auth.get("audience"):
        data["audience"] = auth["audience"]
    if auth.get("account_id"):
        data["account_id"] = auth["account_id"]
    response = client.post(
        token_url,
        data=data,
        auth=(credential.get("client_id", ""), credential.get("client_secret", "")),
    )
    response.raise_for_status()
    value = response.json().get("access_token")
    if not isinstance(value, str):
        raise ExecutionError("authorization server returned no access token")
    return value


def _apply_auth(
    client: httpx.Client,
    auth: dict[str, Any],
    resolved: Mapping[str, str] | str | None,
    headers: dict[str, str],
    query: dict[str, Any],
) -> None:
    if auth["type"] == "none":
        return
    credential = _credential_mapping(resolved or "")
    if auth["type"] == "api_key":
        value = credential.get("value", "")
        if auth.get("header"):
            prefix = f"{auth.get('scheme')} " if auth.get("scheme") else ""
            headers[auth["header"]] = prefix + value
        else:
            query[auth["query"]] = value
    elif auth["type"] == "basic":
        encoded = base64.b64encode(
            f"{credential.get('username', '')}:{credential.get('password', '')}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {encoded}"
    elif auth["type"] == "bearer":
        headers["Authorization"] = f"Bearer {credential.get('value', '')}"
    else:
        headers["Authorization"] = f"Bearer {_token(client, auth, credential)}"


def _project(body: Any, expose: Mapping[str, str]) -> dict[str, Any]:
    result = {}
    for name, source in expose.items():
        path = source.removeprefix("body.") if source != "body" else ""
        try:
            result[name] = _lookup(body, path)
        except (KeyError, IndexError, ValueError, TypeError):
            result[name] = None
    return result


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[credential withheld]")
    return value


class IntegrationExecutor:
    def __init__(
        self,
        compiled: Mapping[str, Any],
        resolver: CredentialResolver = environment_resolver,
        transport: httpx.BaseTransport | None = None,
        reviewed: bool = False,
    ) -> None:
        self.compiled = compiled
        self.resolver = resolver
        self.transport = transport
        self.reviewed = reviewed

    def capabilities(self, phase: str | None = None) -> list[str]:
        integrations = self.compiled["workflow"]["spec"].get("integrations", {})
        all_names = sorted(
            f"{integration}.{operation}"
            for integration, value in integrations.items()
            for operation in value["operations"]
        )
        all_names.extend(
            f"{integration}.request"
            for integration, value in integrations.items()
            if value["access"]["mode"] == "full"
        )
        if phase is None:
            return all_names
        policy = self.compiled["instructions"]["phases"].get(phase)
        if policy is None:
            raise IntegrationError(
                "integration.phase_not_found",
                f"workflow has no phase {phase}",
                category="configuration",
            )
        return list(policy.get("capabilities", []))

    def describe(self, capability: str) -> dict[str, Any]:
        integration_name, separator, operation_name = capability.partition(".")
        if not separator:
            raise IntegrationError(
                "integration.capability_invalid",
                "capability must be integration.operation",
                category="validation",
            )
        integration = (
            self.compiled["workflow"]["spec"].get("integrations", {}).get(integration_name)
        )
        if integration is None:
            raise IntegrationError(
                "integration.not_found",
                f"unknown integration {integration_name}",
                category="configuration",
            )
        operation = integration["operations"].get(operation_name)
        if operation_name == "request" and integration["access"]["mode"] == "full":
            return {
                "name": capability,
                "description": "Make an authorized request to the integration's fixed origin.",
                "input": {
                    "type": "object",
                    "required": ["method", "path"],
                    "properties": {
                        "method": {"enum": integration["access"]["methods"]},
                        "path": {"type": "string", "pattern": "^/[^/].*|^/$"},
                        "query": {"type": "object"},
                        "headers": {"type": "object"},
                        "body": {},
                        "purpose": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
                "output": sorted(integration["access"]["expose"]),
                "policy": {
                    "side_effect": "execute",
                    "approval": "inherit",
                    "idempotency": "none",
                },
            }
        if operation is None:
            raise IntegrationError(
                "integration.capability_not_found",
                f"unknown capability {capability}",
                category="configuration",
            )
        return {
            "name": capability,
            "description": operation["description"],
            "input": operation["input"],
            "output": sorted(operation["response"]["expose"]),
            "policy": operation["policy"],
        }

    def dry_run(self, phase: str) -> dict[str, Any]:
        """Describe the phase's authorized effects without resolving credentials or doing I/O."""
        phase_policy = self.compiled["instructions"]["phases"].get(phase)
        if phase_policy is None:
            raise IntegrationError(
                "integration.phase_not_found",
                f"workflow has no phase {phase}",
                category="configuration",
            )
        return {
            "phase": phase,
            "workflow_revision": self.compiled["workflow_revision"],
            "api": [self.describe(name) for name in self.capabilities(phase)],
            "humans": [
                {"timing": timing, **hook}
                for timing in ("before", "during", "after")
                for hook in phase_policy["humans"][timing]
            ],
            "credentials_resolved": False,
            "requests_executed": False,
        }

    def execute(self, capability: str, inputs: Mapping[str, Any], *, phase: str) -> dict[str, Any]:
        if capability not in self.capabilities(phase):
            raise IntegrationError(
                "integration.capability_denied",
                f"capability {capability} is not authorized for phase {phase}",
                category="policy",
            )
        integration_name, operation_name = capability.split(".", 1)
        spec = self.compiled["workflow"]["spec"]
        integration = spec["integrations"][integration_name]
        if not self.reviewed and (
            integration.get("policy")
            or "max_requests" in integration["access"]
            or integration["access"].get("opaque_identifiers")
        ):
            raise IntegrationError(
                "integration.policy_runtime_unavailable",
                "policy-reviewed execution is not wired yet; no request was sent",
                category="configuration",
            )
        operation = integration["operations"].get(operation_name)
        if operation_name == "request" and integration["access"]["mode"] == "full":
            operation = {
                "description": "Dynamic request inside an authorized origin.",
                "input": self.describe(capability)["input"],
                "request": {
                    "method": inputs.get("method"),
                    "path": inputs.get("path"),
                    "headers": inputs.get("headers", {}),
                    "query": inputs.get("query", {}),
                    **({"body": inputs["body"]} if "body" in inputs else {}),
                    "timeout_seconds": 30,
                    "_dynamic": True,
                },
                "response": {"expose": integration["access"]["expose"]},
                "policy": {
                    "side_effect": "execute",
                    "approval": "inherit",
                    "idempotency": "none",
                },
            }
            forbidden = {
                name.lower()
                for name in operation["request"]["headers"]
                if name.lower() in {"authorization", "cookie", "host", "proxy-authorization"}
            }
            if forbidden:
                raise ExecutionError(
                    f"integration request cannot set protected headers: {', '.join(sorted(forbidden))}"
                )
        if operation is None:
            raise IntegrationError(
                "integration.capability_not_found",
                f"unknown capability {capability}",
                category="configuration",
            )
        try:
            jsonschema.validate(inputs, operation["input"])
        except jsonschema.ValidationError as exc:
            raise IntegrationError(
                "integration.input_invalid",
                f"integration input is invalid: {exc.message}",
                category="validation",
            ) from exc
        connection = next(
            item for item in spec["connections"] if item["ref"] == integration["connection"]
        )
        request = operation["request"]
        dynamic = request.get("_dynamic", False)
        path = request["path"] if dynamic else _render(request["path"], inputs, path_value=True)
        url = urljoin(connection["base_url"] + "/", str(path).lstrip("/"))
        if urlsplit(url).netloc != urlsplit(connection["base_url"]).netloc:
            raise IntegrationError(
                "integration.origin_escape_denied",
                "integration request escaped its configured origin",
                category="policy",
            )
        _safe_destination(url, connection["allow_private_network"])
        headers = (
            {str(key): str(value) for key, value in request["headers"].items()}
            if dynamic
            else {
                str(key): str(_render(value, inputs)) for key, value in request["headers"].items()
            }
        )
        query = request.get("query", {}) if dynamic else _render(request.get("query", {}), inputs)
        resolved = (
            self.resolver(connection["auth"]["credential"])
            if connection["auth"]["type"] != "none"
            else None
        )
        started = time.monotonic()
        sensitive = list(_credential_mapping(resolved).values()) if resolved else []
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=request["timeout_seconds"],
                follow_redirects=False,
            ) as client:
                _apply_auth(client, connection["auth"], resolved, headers, query)
                if connection["auth"]["type"] != "none":
                    sensitive.extend(
                        value
                        for key, value in headers.items()
                        if key.lower() == "authorization" or key == connection["auth"].get("header")
                    )
                    authorization = headers.get("Authorization", "")
                    if " " in authorization:
                        sensitive.append(authorization.split(" ", 1)[1])
                response = client.request(
                    request["method"],
                    url,
                    headers=headers,
                    params=query,
                    json=(
                        request["body"]
                        if dynamic and "body" in request
                        else _render(request["body"], inputs)
                        if "body" in request
                        else None
                    ),
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            category = "authorization" if status in {401, 403} else "transport"
            raise IntegrationError(
                "integration.authorization_failed"
                if category == "authorization"
                else "integration.http_failed",
                f"integration request returned HTTP {status}",
                category=category,
                retryable=status == 429 or status >= 500,
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError(
                "integration.transport_failed",
                "integration request failed",
                category="transport",
                retryable=True,
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text}
        body = _redact(body, [item for item in sensitive if isinstance(item, str)])
        if isinstance(body, dict) and body.get("ok") is False:
            provider_code = body.get("error")
            safe_code = (
                provider_code
                if isinstance(provider_code, str)
                and re.fullmatch(r"[a-z0-9_]{1,64}", provider_code)
                else None
            )
            raise IntegrationError(
                "integration.provider_rejected",
                "provider rejected the request"
                + (f" ({safe_code})" if safe_code is not None else ""),
                category="transport",
            )
        duration = int((time.monotonic() - started) * 1000)
        return {
            "ok": True,
            "status": response.status_code,
            "output": _project(body, operation["response"]["expose"]),
            "duration_ms": duration,
            "audit": {
                "capability": capability,
                "connection": connection["ref"],
                "method": request["method"],
                "origin": connection["base_url"],
                "workflow_revision": self.compiled["workflow_revision"],
                "phase": phase,
                "policy": operation["policy"],
            },
        }


def doctor(
    compiled: dict[str, Any],
    *,
    connectivity: bool = False,
    resolver: CredentialResolver | None = None,
) -> dict[str, Any]:
    """Inspect configuration and optional reachability without disclosing credentials."""
    checks: list[dict[str, Any]] = []
    for connection in compiled["workflow"]["spec"].get("connections", []):
        if connection.get("provider") != "http":
            continue
        auth = connection["auth"]
        reference = auth.get("credential")
        configured = True
        if isinstance(reference, str):
            if resolver is not None:
                try:
                    resolver(reference)
                except ExecutionError:
                    configured = False
            elif reference.startswith("env:"):
                configured = bool(os.environ.get(reference.removeprefix("env:")))
        checks.append(
            {
                "check": "credential_reference",
                "connection": connection["ref"],
                "status": "pass" if configured else "fail",
                "credential_type": auth["type"],
            }
        )
        if connectivity:
            try:
                _safe_destination(
                    connection["base_url"], connection.get("allow_private_network", False)
                )
                response = httpx.head(connection["base_url"], timeout=5, follow_redirects=False)
                reachable = response.status_code < 500
                detail = f"HTTP {response.status_code}"
            except (httpx.HTTPError, ExecutionError):
                reachable, detail = False, "unreachable"
            checks.append(
                {
                    "check": "connectivity",
                    "connection": connection["ref"],
                    "status": "pass" if reachable else "fail",
                    "detail": detail,
                }
            )
    failed = sum(check["status"] == "fail" for check in checks)
    return {
        "ok": failed == 0,
        "workflow_revision": compiled["workflow_revision"],
        "checks": checks,
        "summary": {"passed": len(checks) - failed, "failed": failed},
        "credentials_exposed": False,
    }


def propose_patch(
    config: Path,
    integration: str,
    operation: str,
    definition: Mapping[str, Any],
    *,
    reason: str,
    run: str,
    phase: str,
    agent: str,
) -> dict[str, Any]:
    compiled = compile_workflow(config)
    return {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeWorkflowPatch",
        "metadata": {
            "workflow": compiled["workflow"]["metadata"]["name"],
            "parentRevision": compiled["workflow_revision"],
            "parentContentSha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "derivedFrom": {"run": run, "phase": phase, "agent": agent},
            "reason": reason,
        },
        "spec": {"operations": {"add": {f"{integration}.{operation}": dict(definition)}}},
    }


def import_openapi(
    config: Path,
    integration_name: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    compiled = compile_workflow(config)
    integration = compiled["workflow"]["spec"].get("integrations", {}).get(integration_name)
    if integration is None or integration["access"]["mode"] != "openapi":
        raise ConfigError(f"integration {integration_name} is not configured for OpenAPI")
    source = integration["access"]["source"]
    connection = next(
        item
        for item in compiled["workflow"]["spec"]["connections"]
        if item["ref"] == integration["connection"]
    )
    _safe_destination(source, connection["allow_private_network"])
    try:
        with httpx.Client(transport=transport, timeout=30, follow_redirects=False) as client:
            response = client.get(source)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExecutionError("OpenAPI document could not be fetched", retryable=True) from exc
    try:
        document = yaml.safe_load(response.text)
    except yaml.YAMLError as exc:
        raise ConfigError("OpenAPI document is not valid JSON or YAML") from exc
    if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
        raise ConfigError("only OpenAPI 3 documents are supported")
    wanted = set(integration["access"]["operations"])
    found: dict[str, Any] = {}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        shared_parameters = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.upper() not in {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}:
                continue
            if not isinstance(operation, dict) or operation.get("operationId") not in wanted:
                continue
            operation_id = operation["operationId"]
            name = re.sub(r"[^a-z0-9_-]+", "_", operation_id.lower()).strip("_")
            parameters = [
                item
                for item in [*shared_parameters, *operation.get("parameters", [])]
                if isinstance(item, dict) and "$ref" not in item
            ]
            path_properties = {
                item["name"]: item.get("schema", {})
                for item in parameters
                if item.get("in") == "path" and isinstance(item.get("name"), str)
            }
            path_required = [
                item["name"]
                for item in parameters
                if item.get("in") == "path" and item.get("required")
            ]
            rendered_path = path
            for parameter in path_properties:
                rendered_path = rendered_path.replace(
                    "{" + parameter + "}", "{{ input.path." + parameter + " }}"
                )
            input_properties: dict[str, Any] = {
                "path": {
                    "type": "object",
                    "properties": path_properties,
                    "required": path_required,
                    "additionalProperties": False,
                },
                "query": {"type": "object", "additionalProperties": True},
            }
            required = ["path", "query"]
            request: dict[str, Any] = {
                "method": method.upper(),
                "path": rendered_path,
                "query": "{{ input.query }}",
            }
            body = operation.get("requestBody")
            if isinstance(body, dict):
                body_schema = (
                    body.get("content", {})
                    .get("application/json", {})
                    .get("schema", {"type": "object"})
                )
                input_properties["body"] = body_schema
                request["body"] = "{{ input.body }}"
                if body.get("required"):
                    required.append("body")
            found[name] = {
                "description": operation.get("summary")
                or operation.get("description")
                or operation_id,
                "input": {
                    "type": "object",
                    "properties": input_properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "request": request,
                "response": {"expose": {"result": "body"}},
            }
            wanted.remove(operation_id)
    if wanted:
        raise ConfigError(f"OpenAPI operations were not found: {', '.join(sorted(wanted))}")
    return {
        "apiVersion": "outcomeci.dev/v1alpha1",
        "kind": "OutcomeWorkflowPatch",
        "metadata": {
            "workflow": compiled["workflow"]["metadata"]["name"],
            "parentRevision": compiled["workflow_revision"],
            "parentContentSha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "derivedFrom": {"source": source, "agent": "oci"},
            "reason": f"Import allowlisted operations for {integration_name}",
        },
        "spec": {
            "operations": {
                "add": {
                    f"{integration_name}.{operation}": definition
                    for operation, definition in found.items()
                }
            }
        },
    }


def apply_patch(config: Path, patch_path: Path, output: Path) -> dict[str, Any]:
    compiled = compile_workflow(config)
    patch = yaml.safe_load(patch_path.read_text(encoding="utf-8"))
    if patch.get("kind") != "OutcomeWorkflowPatch":
        raise ConfigError("patch kind must be OutcomeWorkflowPatch")
    if patch.get("metadata", {}).get("parentRevision") != compiled["workflow_revision"]:
        raise ConfigError("patch parent revision is stale")
    document = copy.deepcopy(compiled["workflow"])
    for name, definition in patch.get("spec", {}).get("operations", {}).get("add", {}).items():
        integration, separator, operation = name.partition(".")
        if not separator or integration not in document["spec"].get("integrations", {}):
            raise ConfigError(f"patch operation has unknown integration: {name}")
        operations = document["spec"]["integrations"][integration].setdefault("operations", {})
        if operation in operations:
            raise ConfigError(f"patch operation already exists: {name}")
        operations[operation] = definition
    output.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    child = compile_workflow(output)
    return {
        "parent_revision": compiled["workflow_revision"],
        "workflow_revision": child["workflow_revision"],
        "output": str(output),
        "provenance": patch["metadata"].get("derivedFrom", {}),
    }
