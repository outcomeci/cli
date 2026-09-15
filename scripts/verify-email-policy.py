"""Exercise real local agents against a synthetic Slack-shaped HTTP server.

Uses no real Slack credential, does not send external messages, and preserves
the workflow's intent-driven phase. Only the connection origin changes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from outcomeci import local, local_vault


class SlackShape(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else {}
        self.calls.append({"method": self.command, "path": self.path, "body": body})
        if self.headers.get("Authorization") != "Bearer synthetic-proof-token":
            result = {"ok": False, "error": "invalid_auth"}
        elif self.path.split("?")[0] in {
            "/api/users.list",
            "/api/users.info",
            "/api/users.lookupByEmail",
        }:
            user = {
                "id": "U0123456789",
                "name": "izzy",
                "deleted": False,
                "is_bot": False,
                "real_name": "Izzy",
                "profile": {"display_name": "Izzy", "email": "izzy@example.com"},
            }
            result = {
                "ok": True,
                "members": [user],
                "user": user,
                "response_metadata": {"next_cursor": ""},
            }
        elif self.path == "/api/conversations.open" and body.get("users") == "U0123456789":
            result = {"ok": True, "channel": {"id": "D0123456789"}}
        elif self.path == "/api/chat.postMessage" and body.get("channel") == "D0123456789":
            result = {
                "ok": True,
                "channel": "D0123456789",
                "ts": "1234.567",
                "message": {"text": body.get("text")},
            }
        else:
            result = {"ok": False, "error": "invalid_arguments"}
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "..").resolve()
    workspace = Path(tempfile.mkdtemp(prefix="oci-email-proof-"))
    os.environ["OUTCOMECI_CONFIG_HOME"] = str(workspace / "private")
    shutil.copytree(
        source / ".outcomeci" / "instructions", workspace / ".outcomeci" / "instructions"
    )
    shutil.copytree(source / ".outcomeci" / "schemas", workspace / ".outcomeci" / "schemas")
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlackShape)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    document = yaml.safe_load((source / "outcome.yml").read_text())
    connection = document["spec"]["connections"]["slack"]
    connection["base_url"] = f"http://127.0.0.1:{server.server_port}"
    connection["allow_private_network"] = True
    (workspace / "outcome.yml").write_text(yaml.safe_dump(document, sort_keys=False))
    local_vault.initialize(workspace)
    local_vault.put(workspace, "slack/bot-token", "synthetic-proof-token")
    print(f"Proof workspace: {workspace}", flush=True)
    try:
        result = local.trigger(
            workspace,
            workspace / "outcome.yml",
            "inbound",
            json.loads((source / "email.example.json").read_text()),
        )
        messages = [call for call in SlackShape.calls if call["path"] == "/api/chat.postMessage"]
        assert len(messages) == 1, f"Expected exactly one message, got {len(messages)}"
        assert messages[0]["body"]["channel"] == "D0123456789"
        assert "notify" in result["completed_phases"]
        print(
            json.dumps(
                {
                    "status": "passed",
                    "run_id": result["run_id"],
                    "requests": len(SlackShape.calls),
                    "message_count": len(messages),
                    "workspace": str(workspace),
                },
                indent=2,
            ),
            flush=True,
        )
    finally:
        (workspace / "http-calls.json").write_text(json.dumps(SlackShape.calls, indent=2))
        server.shutdown()


if __name__ == "__main__":
    main()
