"""Custom human-message transports with a fixed OutcomeCI boundary."""
from __future__ import annotations

import json
import os
import select
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .process import ExecutionError

REQUEST_INPUT = {
    "type": "object",
    "required": ["schema_version", "run_id", "phase", "interaction_id", "interaction", "purpose", "targets"],
    "properties": {"targets": {"type": "array", "minItems": 1}},
}
REQUEST_OUTPUT = {"type": "object", "required": ["correlation_id"], "properties": {"correlation_id": {"type": "string", "minLength": 1}}}
POLL_INPUT = {"type": "object", "required": ["correlation_id"], "properties": {"correlation_id": {"type": "string", "minLength": 1}}}
POLL_OUTPUT = {
    "type": "object",
    "required": ["responses"],
    "properties": {
        "responses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["from", "message", "responded_at"],
                "properties": {key: {"type": "string", "minLength": 1} for key in ("from", "message", "responded_at")},
            },
        }
    },
}


def _connection(config: Path, ref: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        connections = document["spec"].get("connections", [])
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise ExecutionError(f"could not read custom connection {ref}: {exc}") from exc
    match = next((item for item in connections if isinstance(item, dict) and item.get("ref") == ref), None)
    if not isinstance(match, dict) or match.get("provider") != "custom":
        raise ExecutionError(f"custom connection {ref} was not found")
    return match


def _headers(connection: dict[str, Any]) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    auth = connection.get("auth", {})
    if isinstance(auth, dict) and auth.get("env"):
        value = os.environ.get(str(auth["env"]))
        if not value:
            raise ExecutionError(f"custom connection needs environment variable {auth['env']}")
        scheme = str(auth.get("scheme", "Bearer"))
        headers[str(auth.get("header", "Authorization"))] = f"{scheme} {value}" if scheme else value
    return headers


def _decode(body: bytes, content_type: str = "") -> dict[str, Any]:
    text = body.decode("utf-8")
    if not text.strip():
        return {}
    if "text/event-stream" in content_type or text.lstrip().startswith("data:"):
        payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:") and line[5:].strip()]
        text = next((item for item in reversed(payloads) if item != "[DONE]"), "")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExecutionError("custom human transport returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ExecutionError("custom human transport must return a JSON object")
    return value


def _post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(url, json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return _decode(response.read(), response.headers.get("Content-Type", "")), dict(response.headers)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExecutionError(f"custom human transport request failed: {exc}", True) from exc


def _http(connection: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    transport = connection["transport"]
    operation_config = connection["operations"][operation]
    base = str(transport["endpoint"]).rstrip("/")
    path = str(operation_config.get("path", "")).format(correlation_id=urllib.parse.quote(str(payload.get("correlation_id", "")), safe=""))
    url = base + (path if path.startswith("/") else f"/{path}")
    method = str(operation_config.get("method", "POST")).upper()
    headers = _headers(connection)
    data = json.dumps(payload).encode() if method != "GET" else None
    if method == "GET" and payload:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(payload)
    request = urllib.request.Request(url, data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return _decode(response.read(), response.headers.get("Content-Type", ""))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExecutionError(f"custom human transport request failed: {exc}", True) from exc


def _mcp_result(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("error"):
        raise ExecutionError(f"custom MCP tool failed: {value['error']}")
    result = value.get("result", {})
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    if isinstance(result, dict):
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                return _decode(str(item.get("text", "")).encode())
    raise ExecutionError("custom MCP tool returned no structured result")


def _mcp_http(connection: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(connection["transport"]["endpoint"])
    headers = _headers(connection)
    initialized, response_headers = _post(endpoint, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "outcomeci", "version": "1"}}}, headers)
    if initialized.get("error"):
        raise ExecutionError(f"custom MCP initialization failed: {initialized['error']}")
    session = response_headers.get("Mcp-Session-Id") or response_headers.get("mcp-session-id")
    if session:
        headers["Mcp-Session-Id"] = session
    _post(endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
    tool = str(connection["operations"][operation].get("tool", f"human_{operation}"))
    result, _ = _post(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": payload}}, headers)
    return _mcp_result(result)


def _mcp_stdio(connection: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    command = connection["transport"].get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ExecutionError("custom MCP stdio transport requires a command list")
    tool = str(connection["operations"][operation].get("tool", f"human_{operation}"))
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "outcomeci", "version": "1"}}}
    tool_call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": payload}}
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        assert process.stdin is not None and process.stdout is not None

        def exchange(message: dict[str, Any], response_id: int) -> dict[str, Any]:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                line = process.stdout.readline()
                if not line:
                    break
                value = _decode(line.encode())
                if value.get("id") == response_id:
                    return value
            raise ExecutionError("custom MCP stdio transport timed out", True)

        initialized = exchange(initialize, 1)
        if initialized.get("error"):
            raise ExecutionError(f"custom MCP initialization failed: {initialized['error']}")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}, separators=(",", ":")) + "\n")
        process.stdin.flush()
        response = exchange(tool_call, 2)
    except OSError as exc:
        raise ExecutionError(f"custom MCP stdio transport failed: {exc}", True) from exc
    finally:
        if "process" in locals():
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return _mcp_result(response)


def call(config: Path, operation: str, payload: dict[str, Any], connection_ref: str) -> dict[str, Any]:
    connection = _connection(config, connection_ref)
    if operation not in {"request", "poll"}:
        raise ExecutionError(f"unsupported custom human operation: {operation}")
    contract = connection.get("contract", {}).get(operation, {}) if isinstance(connection.get("contract", {}), dict) else {}
    input_schemas = [REQUEST_INPUT if operation == "request" else POLL_INPUT]
    if isinstance(contract, dict) and isinstance(contract.get("input"), dict):
        input_schemas.append(contract["input"])
    try:
        for schema in input_schemas:
            jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise ExecutionError(f"custom human request violates its contract: {exc.message}") from exc
    transport = connection.get("transport", {})
    kind = transport.get("type") if isinstance(transport, dict) else None
    if kind == "http":
        result = _http(connection, operation, payload)
    elif kind == "mcp" and transport.get("protocol") == "streamable_http":
        result = _mcp_http(connection, operation, payload)
    elif kind == "mcp" and transport.get("protocol") == "stdio":
        result = _mcp_stdio(connection, operation, payload)
    else:
        raise ExecutionError("unsupported custom human transport")
    output_schemas = [REQUEST_OUTPUT if operation == "request" else POLL_OUTPUT]
    if isinstance(contract, dict) and isinstance(contract.get("output"), dict):
        output_schemas.append(contract["output"])
    try:
        for schema in output_schemas:
            jsonschema.validate(result, schema)
    except jsonschema.ValidationError as exc:
        raise ExecutionError(f"custom human response violates its contract: {exc.message}") from exc
    return result
