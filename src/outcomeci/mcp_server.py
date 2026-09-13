"""Minimal stdio MCP projection for phase-scoped OutcomeCI capabilities."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any, TextIO

from .capability import invoke_integration
from .integrations import IntegrationError, IntegrationExecutor


def _tool_name(capability: str) -> str:
    return capability.replace(".", "__")


def _tools(executor: IntegrationExecutor, phase: str) -> list[dict[str, Any]]:
    result = []
    for capability in executor.capabilities(phase):
        contract = executor.describe(capability)
        policy = contract["policy"]
        result.append(
            {
                "name": _tool_name(capability),
                "title": capability,
                "description": contract["description"] or f"Execute {capability}",
                "inputSchema": contract["input"],
                "annotations": {
                    "readOnlyHint": policy["side_effect"] == "read",
                    "destructiveHint": policy["side_effect"] == "delete",
                    "idempotentHint": policy["idempotency"] in {"supported", "required"},
                },
            }
        )
    return result


def serve(
    executor: IntegrationExecutor,
    phase: str,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    invoke: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    capabilities = {_tool_name(name): name for name in executor.capabilities(phase)}
    invoke = invoke or (
        invoke_integration
        if os.environ.get("OUTCOMECI_CAPABILITY_SOCKET")
        else lambda name, arguments: executor.execute(name, arguments, phase=phase)
    )
    for line in input_stream:
        if not line.strip():
            continue
        request: dict[str, Any] = {}
        try:
            request = json.loads(line)
            method, request_id = request.get("method"), request.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "outcomeci", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": _tools(executor, phase)}
            elif method == "tools/call":
                params = request.get("params", {})
                capability = capabilities.get(params.get("name"))
                if capability is None:
                    raise IntegrationError(
                        "integration.capability_denied",
                        "MCP tool is not authorized for this phase",
                        category="policy",
                    )
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise IntegrationError(
                        "integration.input_invalid",
                        "MCP tool arguments must be an object",
                        category="validation",
                    )
                value = invoke(capability, arguments)
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
                    "structuredContent": value,
                    "isError": False,
                }
            elif request_id is None:
                continue
            else:
                raise IntegrationError(
                    "integration.mcp_method_not_found",
                    f"unsupported MCP method {method}",
                    category="validation",
                )
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (IntegrationError, json.JSONDecodeError) as exc:
            error = (
                exc.as_dict()
                if isinstance(exc, IntegrationError)
                else {
                    "code": "integration.mcp_invalid_json",
                    "category": "validation",
                    "message": "MCP request must be valid JSON",
                    "retryable": False,
                }
            )
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32602, "message": error["message"], "data": error},
            }
        output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
        output_stream.flush()
