"""The one piece shared by this subpackage's local mock HTTP servers.

Each proof that needs a fake remote endpoint (the broker credential check
in step.py, the OAuth-style authorization server in credentials.py, the
cloud Vault API in cloud_vault.py) hand-rolls its own BaseHTTPRequestHandler
with its own routes and auth scheme -- that part genuinely differs per
proof. Only the "send a JSON response" boilerplate was identical across all
three, so only that moved here.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any


def send_json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    encoded = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)
