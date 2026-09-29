"""Compile lowered phase-graph fixtures, the shape every workflow compiles to.

Executor tests describe integrations directly in this shape, so they cover
auth types and access modes no connector exposes yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from outcomeci.config import compile_lowered


def compile_file(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return compile_lowered(document, path)


def email_payload() -> dict[str, Any]:
    from copy import deepcopy

    from outcomeci.contracts import contract_schema

    return deepcopy(contract_schema("email.received")["examples"][0])


def email_notify(root: Path) -> Path:
    """A lowered workflow whose `notify` phase may call a reviewed Slack API.

    The integration uses full access, a request budget, opaque identifiers and
    an integration-level policy reviewer, so broker tests reach every guard."""
    (root / "policy.md").write_text("Review only authorized email notification requests.\n")
    document = {
        "apiVersion": "outcomeci.workflow/v1",
        "kind": "OutcomeWorkflow",
        "metadata": {"name": "email-notify"},
        "spec": {
            "triggers": {"email": {"type": "email.received"}},
            "backend": {"provider": "outcomeci"},
            "context": {"provider": "outcomeci"},
            "instructions": {"workflow": {"content": "Notify the requester."}},
            "agents": {
                "default": {"runner": "codex", "model": "default-model"},
                "phases": {
                    "notify": {
                        "needs": [],
                        "instructions": {"content": "Notify the recipient."},
                        "capabilities": ["slack.request"],
                        "expects": {
                            "inputs": [],
                            "outputs": [
                                {
                                    "name": "delivery",
                                    "path": "delivery.json",
                                    "media_type": "application/json",
                                    "schema": {
                                        "type": "object",
                                        "required": ["status"],
                                        "properties": {
                                            "status": {"enum": ["delivered", "failed", "uncertain"]}
                                        },
                                    },
                                }
                            ],
                        },
                    }
                },
            },
            "connections": {
                "slack": {
                    "provider": "http",
                    "base_url": "https://slack.com",
                    "auth": {"type": "bearer", "credential": "vault:slack/bot-token"},
                }
            },
            "integrations": {
                "slack": {
                    "connection": "slack",
                    "access": {
                        "mode": "full",
                        "methods": ["GET", "POST"],
                        "max_requests": 8,
                        "opaque_identifiers": True,
                    },
                    "policy": {"instructions": "policy.md"},
                }
            },
        },
    }
    path = root / "outcome.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path
