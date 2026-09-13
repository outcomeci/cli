from __future__ import annotations

import io
import json
from pathlib import Path

import yaml
from test_integrations import workflow

from outcomeci.config import compile_workflow
from outcomeci.integrations import IntegrationExecutor
from outcomeci.mcp_server import serve


def _request(method: str, request_id: int, params: dict | None = None) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )


def test_mcp_lists_and_invokes_only_phase_authorized_tools(tmp_path: Path) -> None:
    executor = IntegrationExecutor(compile_workflow(workflow(tmp_path)))
    source = io.StringIO(
        "\n".join(
            [
                _request("initialize", 1),
                _request("tools/list", 2),
                _request(
                    "tools/call",
                    3,
                    {"name": "tickets__create", "arguments": {"title": "Broken button"}},
                ),
            ]
        )
    )
    output = io.StringIO()
    calls = []
    serve(
        executor,
        "intake",
        input_stream=source,
        output_stream=output,
        invoke=lambda capability, arguments: (
            calls.append((capability, arguments)) or {"output": {"id": "T-1"}}
        ),
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[1]["result"]["tools"][0]["name"] == "tickets__create"
    assert responses[1]["result"]["tools"][0]["annotations"]["readOnlyHint"] is False
    assert responses[2]["result"]["structuredContent"]["output"]["id"] == "T-1"
    assert calls == [("tickets.create", {"title": "Broken button"})]


def test_mcp_denies_a_tool_not_granted_to_phase(tmp_path: Path) -> None:
    config = workflow(tmp_path)
    value = yaml.safe_load(config.read_text())
    value["spec"]["agents"]["phases"]["intake"]["integrations"] = [
        item
        for item in value["spec"]["agents"]["phases"]["intake"]["integrations"]
        if item["type"] == "human"
    ]
    config.write_text(yaml.safe_dump(value, sort_keys=False))
    executor = IntegrationExecutor(compile_workflow(config))
    output = io.StringIO()
    serve(
        executor,
        "intake",
        input_stream=io.StringIO(
            _request("tools/call", 1, {"name": "tickets__create", "arguments": {}})
        ),
        output_stream=output,
    )
    error = json.loads(output.getvalue())["error"]["data"]
    assert error["code"] == "integration.capability_denied"
    assert error["category"] == "policy"
