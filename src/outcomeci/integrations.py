"""Credential-blind execution of workflow-authorized HTTP capabilities."""

from __future__ import annotations

import base64
import difflib
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
import jsonschema

from .auth import Authenticator, AuthError, shape_check
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


class LocalCredentialResolver:
    """Resolve `vault:` references from the checkout's local Vault, else the
    environment, and write a rotated secret back to the local Vault."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __call__(self, reference: str) -> Mapping[str, Any] | str:
        if reference.startswith("vault:"):
            from .local_vault import resolve as resolve_local_vault

            return resolve_local_vault(self.root, reference)
        return environment_resolver(reference)

    def rotate(self, reference: str, secrets: dict[str, str]) -> None:
        if not reference.startswith("vault:"):
            raise ExecutionError("only a local Vault credential can store a rotated secret")
        from .local_vault import rotate as rotate_local_vault

        rotate_local_vault(self.root, reference, secrets)


def local_credential_resolver(root: Path) -> CredentialResolver:
    return LocalCredentialResolver(root)


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


def attachments_path(root: Path, run_id: str) -> Path:
    """Where a run's downloaded files are saved: with its artifacts, which its
    agents read in the workspace and people open in the run's directory."""
    return root / ".outcomeci" / "outcomes" / run_id / "attachments"


def same(actual: Any, granted: Any) -> bool:
    """Whether a value is the granted one, ignoring a channel's leading `#`."""
    if isinstance(actual, str) and isinstance(granted, str):
        return actual.strip().lstrip("#") == granted.strip().lstrip("#")
    return actual == granted


DIFF_LIMIT = 40_000


def _decoded(value: Any, encoding: str) -> str | None:
    """A compared file's text, or None when it is not UTF-8 text."""
    if not isinstance(value, str):
        return None
    if encoding == "text":
        return value
    try:
        return base64.b64decode("".join(value.split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _found(body: Any, source: str) -> Any:
    try:
        return _lookup(body, source.removeprefix("body.") if source != "body" else "")
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _response_grant_problems(body: Any, checks: list[dict[str, Any]]) -> list[str]:
    """Each check of one grant needs its granted value in a list at its paths."""
    problems = []
    for check in checks:
        values = []
        for source in check["paths"]:
            found = _found(body, source)
            if isinstance(found, list):
                values.extend(found)
        if not any(same(item, check["granted"]) for item in values):
            problems.append(f"{check['name']} must be {check['granted']}")
    return problems


def _safe_name(name: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._")
    return cleaned[:100] or "file"


def _project(body: Any, expose: Mapping[str, str]) -> dict[str, Any]:
    result = {}
    for name, source in expose.items():
        path = source.removeprefix("body.") if source != "body" else ""
        try:
            result[name] = _lookup(body, path)
        except (KeyError, IndexError, ValueError, TypeError):
            result[name] = None
    return result


def _diagnostic(event: str, **fields: Any) -> None:
    """Emit a redaction-safe timing marker to stderr.

    An integration call's normal completed/failed event is only written
    after the request finishes, so if the surrounding process is killed
    abruptly mid-call -- observed once: a Slack post that Slack's server
    received and acted on, but whose response our side never got to record
    -- that event never lands anywhere. This gives CloudWatch a durable,
    timestamped record of exactly what was in flight and when, using only
    capability names, timings and status codes -- never request/response
    bodies or credentials.
    """
    print(json.dumps({"event": event, "at": time.time(), **fields}), file=sys.stderr, flush=True)


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


def _rejected(body: Any) -> None:
    """Refuse a provider's own `ok: false` answer, keeping only a safe code."""
    if isinstance(body, dict) and body.get("ok") is False:
        provider_code = body.get("error")
        safe_code = (
            provider_code
            if isinstance(provider_code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", provider_code)
            else None
        )
        raise IntegrationError(
            "integration.provider_rejected",
            "provider rejected the request" + (f" ({safe_code})" if safe_code is not None else ""),
            category="transport",
        )


class IntegrationExecutor:
    def __init__(
        self,
        compiled: Mapping[str, Any],
        resolver: CredentialResolver = environment_resolver,
        transport: httpx.BaseTransport | None = None,
        reviewed: bool = False,
        downloads: Path | None = None,
    ) -> None:
        """`downloads` is where an operation's downloaded files are saved for the
        agent to open; an operation that downloads fails without it."""
        self.compiled = compiled
        self.resolver = resolver
        # A resolver that can write a rotated secret back exposes `rotate`.
        self.authenticator = Authenticator(rotate=getattr(resolver, "rotate", None))
        self.transport = transport
        self.reviewed = reviewed
        self.downloads = downloads

    def _download(
        self,
        client: httpx.Client,
        spec: Mapping[str, Any],
        body: Any,
        headers: Mapping[str, str],
        capability: str,
    ) -> dict[str, Any]:
        """Fetch the file a response points to, from an allowed host only."""
        url = _found(body, spec["url"])
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or parsed.hostname not in spec["hosts"]:
            raise IntegrationError(
                "integration.download_denied",
                f"{capability} may download only from {', '.join(spec['hosts'])}",
                category="policy",
            )
        if self.downloads is None:
            raise IntegrationError(
                "integration.download_unavailable",
                "this run has no place to save downloads",
                category="configuration",
            )
        _safe_destination(url, False)
        authorization = {k: v for k, v in headers.items() if k.lower() == "authorization"}
        limit = int(spec["max_bytes"])
        received = bytearray()
        with client.stream("GET", url, headers=authorization) as response:
            if response.status_code != 200:
                raise IntegrationError(
                    "integration.download_failed",
                    f"download returned HTTP {response.status_code}",
                    category="authorization"
                    if response.status_code in {302, 401, 403}
                    else "transport",
                    retryable=response.status_code == 429 or response.status_code >= 500,
                )
            for chunk in response.iter_bytes():
                received.extend(chunk)
                if len(received) > limit:
                    raise IntegrationError(
                        "integration.download_too_large",
                        f"the file is larger than {limit} bytes",
                        category="validation",
                    )
        name = _found(body, spec["name"])
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        target = self.downloads / f"{digest}-{_safe_name(name)}"
        self.downloads.mkdir(parents=True, exist_ok=True)
        # Written beside the target and renamed over it, so a link planted at
        # the target is replaced, never followed.
        descriptor, temporary = tempfile.mkstemp(dir=self.downloads, prefix=".download-")
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(received)
        Path(temporary).replace(target)
        return {
            "path": str(target),
            "name": str(name or target.name),
            "content_type": str(_found(body, spec["content_type"]) or ""),
            "bytes": len(received),
        }

    def capabilities(self, phase: str | None = None) -> list[str]:
        integrations = self.compiled["workflow"]["spec"].get("integrations", {})
        all_names = sorted(
            f"{integration}.{operation}"
            for integration, value in integrations.items()
            for operation in value["operations"]
        )
        if phase is None:
            return all_names
        policy = self.compiled["instructions"]["phases"].get(phase)
        if policy is None:
            raise IntegrationError(
                "integration.phase_not_found",
                f"workflow has no step {phase}",
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
        if operation is None:
            raise IntegrationError(
                "integration.capability_not_found",
                f"unknown capability {capability}",
                category="configuration",
            )
        if "methods" in operation["request"]:
            connection = next(
                item
                for item in self.compiled["workflow"]["spec"]["connections"]
                if item["ref"] == integration["connection"]
            )
            return {
                "name": capability,
                "description": (
                    f"{operation['description']} path is relative to {connection['base_url']}, "
                    "starting with a single leading slash; methods: "
                    + ", ".join(operation["request"]["methods"])
                    + "."
                ),
                "input": operation["input"],
                "output": sorted(operation["response"]["expose"]),
                "policy": operation["policy"],
            }
        return {
            "name": capability,
            "description": operation["description"],
            "input": operation["input"],
            "output": sorted(operation["response"]["expose"]),
            "policy": operation["policy"],
        }

    def dry_run(self, phase: str) -> dict[str, Any]:
        """Describe the step's authorized effects without resolving credentials or doing I/O."""
        if phase not in self.compiled["instructions"]["phases"]:
            raise IntegrationError(
                "integration.phase_not_found",
                f"workflow has no step {phase}",
                category="configuration",
            )
        return {
            "phase": phase,
            "workflow_revision": self.compiled["workflow_revision"],
            "api": [self.describe(name) for name in self.capabilities(phase)],
            "credentials_resolved": False,
            "requests_executed": False,
        }

    def compared(
        self, capability: str, inputs: Mapping[str, Any], *, phase: str
    ) -> dict[str, Any] | None:
        """A write that replaces a file, as a policy reviewer sees it: a unified
        diff against the file's current copy, which this reads with a GET to the
        same path. None when the operation declares no comparison for the call."""
        integration_name, operation_name = capability.split(".", 1)
        integration = self.compiled["workflow"]["spec"]["integrations"][integration_name]
        operation = integration["operations"].get(operation_name) or {}
        method = str(inputs.get("method", "")).upper()
        path = str(inputs.get("path", "")).split("?", 1)[0]
        rule = next(
            (
                item
                for item in operation.get("compare", [])
                if method in item["methods"] and re.search(item["path"], path)
            ),
            None,
        )
        if rule is None:
            return None
        proposed = _decoded(_found(inputs.get("body"), rule["proposed"]), rule["encoding"])
        if proposed is None:
            return {"path": path, "note": "the proposed content is not readable text"}
        ref = _found(inputs.get("body"), rule["ref"]) if rule.get("ref") else None
        try:
            result = self.execute(
                capability,
                {
                    "method": "GET",
                    "path": path,
                    **({"query": {"ref": ref}} if isinstance(ref, str) and ref else {}),
                },
                phase=phase,
            )
            current = _decoded(
                _found(result["output"].get("result"), rule["current"]), rule["encoding"]
            )
            label = f"{path} at {ref}" if ref else path
        except IntegrationError as exc:
            if "HTTP 404" not in str(exc):
                return {
                    "path": path,
                    "note": "the current file could not be read; showing the proposed content",
                    "proposed": proposed[:DIFF_LIMIT],
                }
            current, label = "", "(a new file)"
        if current is None:
            return {"path": path, "note": "the current file is not readable text"}
        diff = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                proposed.splitlines(keepends=True),
                fromfile=label,
                tofile=f"{path} proposed",
            )
        )
        return {
            "path": path,
            "diff": diff[:DIFF_LIMIT] or "(no change)",
            **({"truncated": True} if len(diff) > DIFF_LIMIT else {}),
        }

    def execute(
        self,
        capability: str,
        inputs: Mapping[str, Any],
        *,
        phase: str,
        response_grants: list[list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Send one authorized request. `response_grants` are the grants a
        request cannot name, checked against the response before any of it is
        used: alternatives, one list of `{name, paths, granted}` checks per
        grant, of which one must hold entirely (`granted` in a list at one of
        `paths`). The result's audit names the alternative that held."""
        if capability not in self.capabilities(phase):
            raise IntegrationError(
                "integration.capability_denied",
                f"capability {capability} is not authorized for step {phase}",
                category="policy",
            )
        integration_name, operation_name = capability.split(".", 1)
        spec = self.compiled["workflow"]["spec"]
        integration = spec["integrations"][integration_name]
        if not self.reviewed and "max_requests" in integration["access"]:
            raise IntegrationError(
                "integration.policy_runtime_unavailable",
                "policy-reviewed execution is not wired yet; no request was sent",
                category="configuration",
            )
        operation = integration["operations"].get(operation_name)
        if operation is not None and "methods" in operation["request"]:
            operation = {
                **operation,
                "request": {
                    "method": inputs.get("method"),
                    "path": inputs.get("path"),
                    "headers": inputs.get("headers", {}),
                    "query": inputs.get("query", {}),
                    **({"body": inputs["body"]} if "body" in inputs else {}),
                    "timeout_seconds": 30,
                    "_dynamic": True,
                },
            }
        if operation is not None and operation.get("deny"):
            method = str(inputs.get("method", "")).upper()
            path = str(inputs.get("path", "")).split("?", 1)[0]
            for rule in operation["deny"]:
                if (not rule.get("methods") or method in rule["methods"]) and re.search(
                    rule["path"], path
                ):
                    raise IntegrationError(
                        "integration.request_denied",
                        f"{capability} does not allow {method} {path}: {rule.get('reason')}",
                        category="policy",
                    )
        if operation is not None and operation["request"].get("_dynamic"):
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
        credential = connection["auth"].get("credential")
        resolved = self.resolver(credential) if credential else None
        started = time.monotonic()
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=request["timeout_seconds"],
                follow_redirects=False,
            ) as client:
                try:
                    sensitive = self.authenticator.apply(
                        client, connection["auth"], resolved, headers, query
                    )
                except AuthError as exc:
                    raise IntegrationError(exc.code, str(exc), category=exc.category) from exc
                _diagnostic(
                    "integration_request_sending",
                    capability=capability,
                    phase=phase,
                    method=request["method"],
                )
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
                _diagnostic(
                    "integration_request_received",
                    capability=capability,
                    phase=phase,
                    status=response.status_code,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError:
                    body = {"text": response.text}
                body = _redact(body, [item for item in sensitive if isinstance(item, str)])
                _rejected(body)
                granted_by = None
                if response_grants:
                    problems: list[str] = []
                    for index, checks in enumerate(response_grants):
                        found = _response_grant_problems(body, checks)
                        if not found:
                            granted_by = index
                            break
                        problems.extend(found)
                    if granted_by is None:
                        raise IntegrationError(
                            "integration.grant_denied",
                            f"{capability} is outside this step's grants: " + "; ".join(problems),
                            category="policy",
                        )
                download = operation["response"].get("download")
                file = (
                    self._download(client, download, body, headers, capability)
                    if download
                    else None
                )
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
        duration = int((time.monotonic() - started) * 1000)
        output = _project(body, operation["response"]["expose"])
        if file is not None:
            output["file"] = file
        return {
            "ok": True,
            "status": response.status_code,
            "output": output,
            "duration_ms": duration,
            "audit": {
                "capability": capability,
                "connection": connection["ref"],
                "method": request["method"],
                "origin": connection["base_url"],
                "workflow_revision": self.compiled["workflow_revision"],
                "phase": phase,
                "policy": operation["policy"],
                **({"grant": granted_by} if granted_by is not None else {}),
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
        resolved: Mapping[str, str] | str | None = None
        if isinstance(reference, str):
            if resolver is not None:
                try:
                    resolved = resolver(reference)
                except ExecutionError:
                    configured = False
            elif reference.startswith("env:"):
                configured = bool(os.environ.get(reference.removeprefix("env:")))
        checks.append(
            {
                "check": "credential_reference",
                "connection": connection["ref"],
                "status": "pass" if configured else "fail",
                "accepts": [entry["kind"] for entry in auth["accepts"]],
            }
        )
        if configured and resolved is not None:
            shape_ok, detail = shape_check(auth, resolved)
            checks.append(
                {
                    "check": "credential_shape",
                    "connection": connection["ref"],
                    "status": "pass" if shape_ok else "fail",
                    "detail": detail,
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
