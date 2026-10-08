import base64
import hashlib
import importlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from outcomeci.cloud_runner.client import CoreClient, CoreError
from outcomeci.cloud_runner.main import (
    CODEX_FILE_AUTH_CONFIG,
    _inject_agent_credential,
    _is_usage_limit_error,
    _write_private_file,
    authorize,
    execute_publication,
    execute_workflow,
    workflow_artifacts,
    workflow_failure_category,
)
from outcomeci.cloud_runner.models import AuthorizationClaim, ContractError, Launch
from outcomeci.cloud_runner.process import ProcessResult
from outcomeci.process import ExecutionError

cloud_runner_main = importlib.import_module("outcomeci.cloud_runner.main")


class FlowTests(unittest.TestCase):
    def test_workflow_artifact_guardrails_are_raised(self):
        self.assertEqual(cloud_runner_main.WORKFLOW_ARTIFACT_FILE_LIMIT, 1_000)
        self.assertEqual(cloud_runner_main.WORKFLOW_ARTIFACT_FILE_BYTES, 32 * 1024 * 1024)
        self.assertEqual(cloud_runner_main.WORKFLOW_ARTIFACT_TOTAL_BYTES, 100 * 1024 * 1024)

    def test_large_workflow_artifact_is_losslessly_chunked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / ".outcomeci" / "outcomes" / "run-1" / "trace.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_bytes(b"abcdefghij")
            with mock.patch.object(cloud_runner_main, "WORKFLOW_ARTIFACT_FILE_BYTES", 4):
                artifacts = workflow_artifacts(root, "run-1")

        parts = sorted(
            (item for item in artifacts if item["path"].endswith(".part")),
            key=lambda item: item["path"],
        )
        manifest_record = next(item for item in artifacts if item["path"].endswith("manifest.json"))
        reassembled = b"".join(base64.b64decode(item["content_base64"]) for item in parts)
        manifest = json.loads(base64.b64decode(manifest_record["content_base64"]))
        self.assertEqual(reassembled, b"abcdefghij")
        self.assertEqual(manifest["schema_version"], "outcomeci.artifact-chunks/v1")
        self.assertEqual(manifest["source_path"], ".outcomeci/outcomes/run-1/trace.jsonl")
        self.assertEqual(manifest["sha256"], hashlib.sha256(reassembled).hexdigest())
        self.assertEqual(len(manifest["parts"]), 3)

    def test_oversized_source_does_not_discard_other_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = root / ".outcomeci" / "outcomes" / "run-1"
            outcome.mkdir(parents=True)
            (outcome / "a-small.json").write_bytes(b"small")
            (outcome / "z-too-large.jsonl").write_bytes(b"x" * 21)
            stderr = io.StringIO()
            with (
                mock.patch.object(cloud_runner_main, "WORKFLOW_ARTIFACT_FILE_BYTES", 10),
                mock.patch.object(cloud_runner_main, "WORKFLOW_ARTIFACT_TOTAL_BYTES", 20),
                mock.patch("sys.stderr", stderr),
            ):
                artifacts = workflow_artifacts(root, "run-1")

        self.assertEqual(
            [item["path"] for item in artifacts],
            [".outcomeci/outcomes/run-1/a-small.json"],
        )
        report = json.loads(stderr.getvalue())
        self.assertEqual(report["omitted_file_count"], 1)
        self.assertEqual(report["omitted_bytes"], 21)

    def test_workflow_failure_categories_do_not_expose_agent_output(self):
        error = ExecutionError("codex failed with exit 1: private provider output", True)

        self.assertEqual(workflow_failure_category(error), "agent_process_failed")

    def test_is_usage_limit_error_matches_common_phrasing(self):
        for message in (
            "codex failed with exit 1: Usage limit reached, try again later",
            "claude failed with exit 1: rate limit exceeded",
            "codex failed with exit 1: 429 Too Many Requests",
            "codex failed with exit 1: monthly quota exceeded",
        ):
            self.assertTrue(_is_usage_limit_error(ExecutionError(message)))
        self.assertFalse(_is_usage_limit_error(ExecutionError("codex failed with exit 1: boom")))
        self.assertFalse(_is_usage_limit_error(ContractError("workflow recorded an error")))

    def test_a_retryable_conflict_is_reported_as_retryable_on_complete(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }

        class WorkflowClient:
            completed = []
            heartbeats = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                options["on_created"]("run-1")
                trace = workspace / ".outcomeci" / "outcomes" / "run-1" / "transcripts"
                trace.mkdir(parents=True)
                (trace / "codex.jsonl").write_text('{"type":"partial"}\n')
                raise CoreError("core_conflict", True)

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"resolve_analytics": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
                self.assertRaises(CoreError),
            ):
                execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )
        self.assertEqual(len(client.completed), 1)
        _, status, values = client.completed[0]
        self.assertEqual(status, "failed")
        self.assertTrue(values["retryable"])
        self.assertEqual(values["artifacts"], [])
        self.assertEqual(values["resource_usage"]["schema_version"], 1)

    def test_a_retryable_claim_conflict_is_a_clean_no_op(self):
        # Losing the race to claim an invocation (or its agent connection
        # being busy) before a lease was ever issued -- there is nothing to
        # thread a retryable flag through to workflow_complete() for, since
        # no lease/completion token exists yet. This is the gap #58 didn't
        # close: it only covers a conflict discovered *after* a successful
        # claim, mid-execution.
        class ClaimConflictClient:
            def claim_workflow(self):
                raise CoreError("lease_conflict", True)

        result = execute_workflow(
            Launch("workflow", "invocation-1", "boot", "https://api.outcomeci.com"),
            ClaimConflictClient(),
        )

        self.assertEqual(result, 0)

    def test_a_non_retryable_claim_failure_still_raises(self):
        class ClaimRejectedClient:
            def claim_workflow(self):
                raise CoreError("claim_rejected", False)

        with self.assertRaises(CoreError):
            execute_workflow(
                Launch("workflow", "invocation-1", "boot", "https://api.outcomeci.com"),
                ClaimRejectedClient(),
            )

    def test_authorize_claim_conflict_is_also_a_clean_no_op(self):
        # The same claim-time conflict #70 fixed for execute_workflow can
        # happen on any of the other three claim_*() entry points, since
        # they all go through the same broker mechanism.
        class ClaimConflictClient:
            def claim_authorization(self):
                raise CoreError("lease_conflict", True)

        result = authorize(
            Launch("authorize", "invocation-1", "boot", "https://api.outcomeci.com"),
            ClaimConflictClient(),
        )

        self.assertEqual(result, 0)

    def test_authorize_non_retryable_claim_failure_still_raises(self):
        class ClaimRejectedClient:
            def claim_authorization(self):
                raise CoreError("claim_rejected", False)

        with self.assertRaises(CoreError):
            authorize(
                Launch("authorize", "invocation-1", "boot", "https://api.outcomeci.com"),
                ClaimRejectedClient(),
            )

    def test_execute_publication_claim_conflict_is_also_a_clean_no_op(self):
        class ClaimConflictClient:
            def claim_publication(self):
                raise CoreError("lease_conflict", True)

        result = execute_publication(
            Launch("publication", "invocation-1", "boot", "https://api.outcomeci.com"),
            ClaimConflictClient(),
        )

        self.assertEqual(result, 0)

    def test_execute_publication_non_retryable_claim_failure_still_raises(self):
        class ClaimRejectedClient:
            def claim_publication(self):
                raise CoreError("claim_rejected", False)

        with self.assertRaises(CoreError):
            execute_publication(
                Launch("publication", "invocation-1", "boot", "https://api.outcomeci.com"),
                ClaimRejectedClient(),
            )

    def test_generic_workflow_uses_scoped_vault_values_and_completes(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {
                "expires_at": "2099-01-01T00:00:00+00:00",
                "values": {
                    "slack/bot-token": {
                        "credential_type": "auth_header",
                        "configuration": {
                            "header_name": "Authorization",
                            "scheme": "Bearer",
                        },
                        "secrets": {"value": "slack-secret"},
                    }
                },
            },
        }

        class WorkflowClient:
            completed = []
            heartbeats = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, *, on_created, options):
                self.assertIs(options._container_isolated, True)
                self.assertEqual(
                    options.credential_resolver("vault:slack/bot-token")["secrets"]["value"],
                    "slack-secret",
                )
                self.assertEqual(name, "inbound")
                on_created("run-1")
                trace = workspace / ".outcomeci" / "outcomes" / "run-1" / "transcripts"
                trace.mkdir(parents=True)
                (trace / "codex.jsonl").write_text('{"type":"event"}\n')
                (workspace / ".outcomeci" / "outcomes" / "run-1" / ".env").write_text(
                    "TOKEN=never-upload\n"
                )
                options.event_sink(
                    {
                        "event_id": "00000000-0000-0000-0000-000000000001",
                        "occurred_at": "2026-09-16T00:00:00+00:00",
                        "event_type": "permission.reviewed",
                        "step": "notify",
                        "capability": "slack.request",
                        "message": "Permission advisor: allow",
                        "decision": "allow",
                    }
                )
                return {
                    "run_id": "run-1",
                    "status": "completed",
                    "completed_steps": ["notify"],
                }

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"notify": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
            ):
                self.assertEqual(
                    execute_workflow(
                        Launch(
                            "workflow",
                            "invocation-1",
                            "boot",
                            "https://api.outcomeci.com",
                        ),
                        client,
                    ),
                    0,
                )
        self.assertEqual(client.completed[0][0:2], ("lease-secret", "completed"))
        self.assertEqual(client.completed[0][2]["run_id"], "run-1")
        artifacts = client.completed[0][2]["artifacts"]
        self.assertEqual(
            [item["path"] for item in artifacts],
            [".outcomeci/outcomes/run-1/transcripts/codex.jsonl"],
        )
        self.assertEqual(
            base64.b64decode(artifacts[0]["content_base64"]),
            b'{"type":"event"}\n',
        )
        self.assertNotIn("never-upload", repr(artifacts))
        self.assertEqual(client.completed[0][2]["expected_credential_version"], 3)
        self.assertEqual(client.completed[0][2]["agent_credential"], {"token": "agent-secret"})
        self.assertEqual(client.completed[0][2]["resource_usage"]["schema_version"], 1)
        self.assertGreaterEqual(client.completed[0][2]["resource_usage"]["sample_count"], 1)
        self.assertEqual(client.heartbeats[0][1][0]["event_type"], "permission.reviewed")

    def test_a_claim_without_an_agent_runs_a_model_only_workflow(self):
        # A workflow whose steps all run on model profiles is claimed with no
        # agent login; the runner reports no credential to write back.
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }

        class WorkflowClient:
            completed = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, *, on_created, options):
                self.assertIsNone(options.agent)
                on_created("run-1")
                (workspace / ".outcomeci" / "outcomes" / "run-1").mkdir(parents=True)
                return {"run_id": "run-1", "status": "completed", "completed_steps": ["digest"]}

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch("outcomeci.cloud_runner.main.tempfile.mkdtemp", return_value=str(root)),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {
                            "steps": {
                                "digest": {
                                    "v1": {"reasoning": {"profile": "d", "model": "anthropic/x"}}
                                }
                            }
                        },
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
            ):
                self.assertEqual(
                    execute_workflow(
                        Launch("workflow", "invocation-1", "boot", "https://api.outcomeci.com"),
                        client,
                    ),
                    0,
                )
        self.assertEqual(client.completed[0][0:2], ("lease-secret", "completed"))
        self.assertIsNone(client.completed[0][2]["expected_credential_version"])
        self.assertIsNone(client.completed[0][2]["agent_credential"])

    def test_generic_workflow_auto_continues_through_ready_steps(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {
                "expires_at": "2099-01-01T00:00:00+00:00",
                "values": {},
            },
        }

        class WorkflowClient:
            completed = []
            heartbeats = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()
            continue_calls = []

            def trigger(workspace, config, name, payload, *, on_created, options):
                on_created("run-1")
                return {
                    "run_id": "run-1",
                    "status": "awaiting_confirmation",
                    "completed_steps": ["resolve_analytics"],
                    "ready_steps": ["notify"],
                }

            def continue_run(root_arg, config_arg, run_id, *, approve, options):
                continue_calls.append((run_id, approve, options))
                self.assertIs(options._container_isolated, True)
                self.assertTrue(callable(options.credential_resolver))
                trace = root_arg / ".outcomeci" / "outcomes" / run_id / "transcripts"
                trace.mkdir(parents=True, exist_ok=True)
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "completed_steps": ["resolve_analytics", "notify"],
                }

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch("outcomeci.local.continue_run", side_effect=continue_run),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"resolve_analytics": {}, "notify": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
            ):
                self.assertEqual(
                    execute_workflow(
                        Launch(
                            "workflow",
                            "invocation-1",
                            "boot",
                            "https://api.outcomeci.com",
                        ),
                        client,
                    ),
                    0,
                )
        self.assertEqual(len(continue_calls), 1)
        self.assertEqual(continue_calls[0][0:2], ("run-1", True))
        self.assertEqual(client.completed[0][0:2], ("lease-secret", "completed"))
        self.assertEqual(client.completed[0][2]["run_id"], "run-1")

    def test_usage_limit_swaps_to_the_declared_fallback_agent_and_stays_on_it(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }

        class WorkflowClient:
            completed = []
            heartbeats = []
            fallback_calls = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_agent_fallback(self, token):
                self.fallback_calls.append(token)
                return {
                    "provider": "claude",
                    "credential": "claude-secret",
                    "credential_version": 9,
                }

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()
            trigger_calls = []
            retry_calls = []
            continue_calls = []

            def trigger(workspace, config, name, payload, *, on_created, options):
                trigger_calls.append(options)
                on_created("run-1")
                raise ExecutionError("codex failed with exit 1: Usage limit reached, try later")

            def retry(root_arg, config_arg, run_id, *, options):
                retry_calls.append(options)
                self.assertEqual(options.agent, "claude")
                self.assertEqual(options.model, "claude-opus-5")
                return {
                    "run_id": run_id,
                    "status": "awaiting_confirmation",
                    "completed_steps": ["resolve_analytics"],
                    "ready_steps": ["notify"],
                }

            def continue_run(root_arg, config_arg, run_id, *, approve, options):
                continue_calls.append(options)
                self.assertEqual(options.agent, "claude")
                trace = root_arg / ".outcomeci" / "outcomes" / run_id / "transcripts"
                trace.mkdir(parents=True, exist_ok=True)
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "completed_steps": ["resolve_analytics", "notify"],
                }

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch("outcomeci.local.retry", side_effect=retry),
                mock.patch("outcomeci.local.continue_run", side_effect=continue_run),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"resolve_analytics": {}, "notify": {}}},
                        "workflow": {
                            "spec": {
                                "agents": {
                                    "default": {
                                        "runner": "codex",
                                        "fallback": {
                                            "runner": "claude",
                                            "model": "claude-opus-5",
                                        },
                                    }
                                }
                            }
                        },
                    },
                ),
            ):
                self.assertEqual(
                    execute_workflow(
                        Launch(
                            "workflow",
                            "invocation-1",
                            "boot",
                            "https://api.outcomeci.com",
                        ),
                        client,
                    ),
                    0,
                )
        self.assertEqual(len(trigger_calls), 1)
        self.assertEqual(len(retry_calls), 1)
        self.assertEqual(len(continue_calls), 1)
        self.assertEqual(client.fallback_calls, ["lease-secret"])
        self.assertEqual(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), None)  # restored
        self.assertEqual(client.completed[0][0:2], ("lease-secret", "completed"))

    def test_fallback_failure_does_not_attach_a_stale_codex_credential_writeback(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }

        class WorkflowClient:
            completed = []
            heartbeats = []
            fallback_calls = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_agent_fallback(self, token):
                self.fallback_calls.append(token)
                return {
                    "provider": "claude",
                    "credential": "claude-secret",
                    "credential_version": 9,
                }

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, *, on_created, options):
                on_created("run-1")
                raise ExecutionError("codex failed with exit 1: Usage limit reached, try later")

            def retry(root_arg, config_arg, run_id, *, options):
                self.assertEqual(options.agent, "claude")
                raise ExecutionError("claude failed with exit 1: authentication rejected")

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch("outcomeci.local.retry", side_effect=retry),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"resolve_analytics": {}, "notify": {}}},
                        "workflow": {
                            "spec": {
                                "agents": {
                                    "default": {
                                        "runner": "codex",
                                        "fallback": {"runner": "claude"},
                                    }
                                }
                            }
                        },
                    },
                ),
                self.assertRaises(ExecutionError),
            ):
                execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )
        self.assertEqual(client.fallback_calls, ["lease-secret"])
        self.assertEqual(len(client.completed), 1)
        token, status, values = client.completed[0]
        self.assertEqual((token, status), ("lease-secret", "failed"))
        self.assertIsNone(values["agent_credential"])
        self.assertEqual(values["expected_credential_version"], 9)

    def test_a_rejected_completion_report_is_logged_instead_of_silently_swallowed(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }

        class WorkflowClient:
            heartbeats = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                raise CoreError("core_conflict")

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                options["on_created"]("run-1")
                raise ExecutionError("codex failed with exit 1: usage limit reached")

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"resolve_analytics": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
                self.assertRaises(ExecutionError),
            ):
                execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )
        reports = [
            json.loads(line)
            for line in stderr.getvalue().splitlines()
            if "workflow_completion_report_failed" in line
        ]
        self.assertEqual(
            reports,
            [
                {
                    "event": "workflow_completion_report_failed",
                    "report_category": "core_conflict",
                    "original_category": "agent_process_failed",
                }
            ],
        )

    def test_non_usage_limit_failure_never_triggers_the_fallback(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }

        class WorkflowClient:
            completed = []
            heartbeats = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_agent_fallback(self, token):
                raise AssertionError("workflow_agent_fallback should not be called")

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                options["on_created"]("run-1")
                raise ExecutionError("codex failed with exit 1: unexpected tool error")

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.local.retry",
                    side_effect=AssertionError("retry should not be called"),
                ),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"notify": {}}},
                        "workflow": {
                            "spec": {
                                "agents": {
                                    "default": {
                                        "runner": "codex",
                                        "fallback": {"runner": "claude"},
                                    }
                                }
                            }
                        },
                    },
                ),
                self.assertRaises(ExecutionError),
            ):
                execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )
        self.assertEqual(client.completed[0][1], "failed")

    def test_generic_workflow_failure_reports_a_redacted_detail(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "trigger_name": "inbound",
            "input": {"subject": "hello"},
            "lease_token": "lease-secret",
            "agent": {
                "provider": "codex",
                "credential": {"token": "agent-secret"},
                "credential_version": 3,
            },
            "vault": {
                "expires_at": "2099-01-01T00:00:00+00:00",
                "values": {},
            },
        }

        class WorkflowClient:
            completed = []
            heartbeats = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token, events=None):
                self.heartbeats.append((token, events or []))
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                trace = workspace / ".outcomeci" / "outcomes" / "run-1" / "transcripts"
                trace.mkdir(parents=True)
                (trace / "codex.jsonl").write_text('{"type":"partial"}\n')
                raise ExecutionError(
                    "request failed: Bearer sk-abc123supersecretlongtoken rejected"
                )

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"notify": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
                self.assertRaises(ExecutionError),
            ):
                execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )

        token, status, values = client.completed[-1]
        self.assertEqual((token, status), ("lease-secret", "failed"))
        self.assertEqual(values["category"], "workflow_execution_failed")
        self.assertNotIn("sk-abc123supersecretlongtoken", values["detail"])
        self.assertIn("[REDACTED]", values["detail"])
        self.assertIn("request failed", values["detail"])
        self.assertEqual(values["run_id"], "run-1")
        self.assertEqual(
            [item["path"] for item in values["artifacts"]],
            [".outcomeci/outcomes/run-1/transcripts/codex.jsonl"],
        )
        self.assertEqual(
            base64.b64decode(values["artifacts"][0]["content_base64"]),
            b'{"type":"partial"}\n',
        )

    def test_claude_authorization_extracts_the_token_from_a_plain_transcript(self):
        claim = AuthorizationClaim(
            provider="claude",
            command=("claude", "setup-token"),
            session_token="session-1",
            expires_at="2099-01-01T00:00:00Z",
        )

        class Client:
            def __init__(self):
                self.completions: list[tuple] = []
                self.failures: list[tuple] = []

            def claim_authorization(self):
                return claim

            def complete(self, *args):
                self.completions.append(args)

            def fail(self, *args):
                self.failures.append(args)

        client = Client()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def fake_run(command, *, cwd, env, timeout, on_output, terminal, input_provider):
                return ProcessResult(
                    0, "Your OAuth token:\r\nsk-ant-oat01-real-token-1234567890\r\n", ""
                )

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=fake_run),
            ):
                self.assertEqual(
                    authorize(
                        Launch("authorize", "job_1", "boot", "https://api.outcomeci.com"),
                        client,
                    ),
                    0,
                )
        self.assertEqual(len(client.completions), 1)
        self.assertEqual(
            client.completions[0][1],
            {"provider": "claude", "credential": "sk-ant-oat01-real-token-1234567890"},
        )
        self.assertEqual(client.failures, [])

    def test_claude_authorization_survives_a_cursor_positioned_redraw_of_the_token(self):
        """Ink-style TUIs redraw via cursor movement rather than printing
        linearly. Naively stripping ANSI codes and concatenating what's left
        can jump straight over characters a prior frame already drew,
        silently truncating the token -- this reproduces that exact
        corruption pattern with a synthetic (non-secret) token and asserts
        the terminal-replay extraction reconstructs it correctly."""
        claim = AuthorizationClaim(
            provider="claude",
            command=("claude", "setup-token"),
            session_token="session-1",
            expires_at="2099-01-01T00:00:00Z",
        )

        class Client:
            def __init__(self):
                self.completions: list[tuple] = []
                self.failures: list[tuple] = []

            def claim_authorization(self):
                return claim

            def complete(self, *args):
                self.completions.append(args)

            def fail(self, *args):
                self.failures.append(args)

        client = Client()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            # First frame draws the full token at column 1; a later frame
            # redraws only a later segment via CHA (cursor horizontal
            # absolute), leaving the earlier characters from the first frame
            # in place -- exactly the pattern observed from a real
            # `claude setup-token` run.
            transcript = (
                "sk-ant-oat01-placeholder-XXXXXXXXXXXXXXXXXX\r\n"
                "\x1b[1A"  # cursor up onto the token's row
                "\x1b[14G"  # jump to column 14, leaving "sk-ant-oat01-" from frame 1
                "\x1b[K"  # erase to end of line before redrawing the rest
                "real-token-1234567890"
            )

            def fake_run(command, *, cwd, env, timeout, on_output, terminal, input_provider):
                return ProcessResult(0, transcript, "")

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=fake_run),
            ):
                self.assertEqual(
                    authorize(
                        Launch("authorize", "job_1", "boot", "https://api.outcomeci.com"),
                        client,
                    ),
                    0,
                )
        self.assertEqual(
            client.completions[0][1],
            {"provider": "claude", "credential": "sk-ant-oat01-real-token-1234567890"},
        )

    def test_claude_authorization_fails_loudly_when_no_token_appears_in_the_transcript(self):
        claim = AuthorizationClaim(
            provider="claude",
            command=("claude", "setup-token"),
            session_token="session-1",
            expires_at="2099-01-01T00:00:00Z",
        )

        class Client:
            def __init__(self):
                self.completions: list[tuple] = []
                self.failures: list[tuple] = []

            def claim_authorization(self):
                return claim

            def complete(self, *args):
                self.completions.append(args)

            def fail(self, *args):
                self.failures.append(args)

        client = Client()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def fake_run(command, *, cwd, env, timeout, on_output, terminal, input_provider):
                return ProcessResult(0, "some terminal output with no credential", "")

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=fake_run),
                self.assertRaises(ContractError),
            ):
                authorize(
                    Launch("authorize", "job_1", "boot", "https://api.outcomeci.com"),
                    client,
                )
        self.assertEqual(client.completions, [])
        self.assertEqual(client.failures, [("session-1", "invalid_result", False)])

    def test_write_private_file_is_never_world_or_group_readable(self):
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "secret.txt"
            _write_private_file(path, "s3cr3t")
            self.assertEqual(path.read_text(encoding="utf-8"), "s3cr3t")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_write_private_file_can_overwrite_in_place(self):
        # Unlike CodexAdapter.hydrate's O_EXCL write, this must tolerate
        # being called twice in the same root -- a fallback to a different
        # agent re-injects a credential after this one already wrote here.
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "secret.txt"
            _write_private_file(path, "first")
            _write_private_file(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "second")

    def test_inject_codex_credential_writes_auth_and_file_auth_config(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            injected = _inject_agent_credential(root, "codex", {"token": "codex-secret"})
            home = root / ".codex"
            self.assertEqual(injected, {"CODEX_HOME": str(home)})
            self.assertEqual(
                json.loads((home / "auth.json").read_text(encoding="utf-8")),
                {"token": "codex-secret"},
            )
            self.assertEqual(
                (home / "config.toml").read_text(encoding="utf-8"), CODEX_FILE_AUTH_CONFIG
            )
            self.assertEqual(stat.S_IMODE((home / "auth.json").stat().st_mode), 0o600)


def publication_claim(agent="codex"):
    hydration = (
        {"provider": "codex", "auth_json": {"token": "codex-secret"}}
        if agent == "codex"
        else {"provider": "claude", "oauth_token": "claude-secret"}
    )
    return {
        "job": {
            "job_id": "pub-1",
            "agent": agent,
            "model": None,
            "source_filename": "outcome.yml",
            "content": "apiVersion: outcomeci.workflow/v1\nname: example\n",
            "files": {},
            "sensitive_terms": [],
        },
        "completion_token": "completion-secret",
        "lease_id": "lease-1",
        "lease_expires_at": "2099-01-01T00:00:00Z",
        "hydration": hydration,
    }


class PublicationClient:
    def __init__(self, claim):
        self.claim = claim
        self.completions = []
        self.failures = []
        self.fallback_calls = []

    def claim_publication(self):
        return self.claim

    def complete_publication(self, token, payload):
        self.completions.append((token, payload))

    def fail(self, token, category, retryable):
        self.failures.append((token, category, retryable))

    def publication_agent_fallback(self, token):
        self.fallback_calls.append(token)
        return {
            "provider": "claude",
            "credential": "claude-fallback-secret",
            "credential_version": 9,
        }


def _fake_prepare_publication_writing_output(source, destination, **_options):
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "outcome.yml").write_text("apiVersion: outcomeci.workflow/v1\nname: example\n")
    (destination / ".outcomeci").mkdir()
    (destination / ".outcomeci/publication-overview.md").write_text("# Public overview\n")
    return {
        "package_digest": "digest-1",
        "workflow_revision": "rev-1",
        "compiler_version": "1",
        "requirements": [],
        "replacement_report": [],
        "workflow_file": "outcome.yml",
    }


class PublicationFallbackTests(unittest.TestCase):
    def test_publication_completes_without_needing_a_fallback(self):
        client = PublicationClient(publication_claim(agent="codex"))
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()
            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.prepare_publication",
                    side_effect=_fake_prepare_publication_writing_output,
                ),
            ):
                result = execute_publication(
                    Launch("publication", "pub-1", "boot", "https://api.outcomeci.com"),
                    client,
                )
        self.assertEqual(result, 0)
        self.assertEqual(client.fallback_calls, [])
        self.assertEqual(len(client.completions), 1)
        self.assertEqual(client.completions[0][0], "completion-secret")
        self.assertEqual(
            base64.b64decode(
                client.completions[0][1]["files"][".outcomeci/publication-overview.md"]
            ).decode(),
            "# Public overview\n",
        )

    def test_a_codex_usage_limit_falls_back_to_claude_and_completes(self):
        client = PublicationClient(publication_claim(agent="codex"))
        attempts = []

        def fake_prepare_publication(source, destination, *, agent, **options):
            attempts.append(agent)
            if agent == "codex":
                raise ExecutionError(
                    "codex failed with exit 1: ERROR: You've hit your usage limit.",
                    True,
                )
            return _fake_prepare_publication_writing_output(
                source, destination, agent=agent, **options
            )

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()
            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.prepare_publication",
                    side_effect=fake_prepare_publication,
                ),
            ):
                result = execute_publication(
                    Launch("publication", "pub-1", "boot", "https://api.outcomeci.com"),
                    client,
                )
        self.assertEqual(result, 0)
        self.assertEqual(attempts, ["codex", "claude"])
        self.assertEqual(client.fallback_calls, ["completion-secret"])
        self.assertEqual(len(client.completions), 1)

    def test_a_claude_usage_limit_does_not_fall_back_again(self):
        client = PublicationClient(publication_claim(agent="claude"))

        def fake_prepare_publication(source, destination, *, agent, **options):
            raise ExecutionError(
                "claude failed with exit 1: usage limit reached, try again later", True
            )

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()
            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.prepare_publication",
                    side_effect=fake_prepare_publication,
                ),
                self.assertRaises(ExecutionError),
            ):
                execute_publication(
                    Launch("publication", "pub-1", "boot", "https://api.outcomeci.com"),
                    client,
                )
        self.assertEqual(client.fallback_calls, [])
        self.assertEqual(client.completions, [])
        self.assertEqual(len(client.failures), 1)
        self.assertEqual(client.failures[0][0:2], ("completion-secret", "agent_process_failed"))

    def test_a_non_usage_limit_failure_does_not_fall_back(self):
        client = PublicationClient(publication_claim(agent="codex"))

        def fake_prepare_publication(source, destination, *, agent, **options):
            raise ExecutionError("codex failed with exit 1: syntax error", False)

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()
            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.prepare_publication",
                    side_effect=fake_prepare_publication,
                ),
                self.assertRaises(ExecutionError),
            ):
                execute_publication(
                    Launch("publication", "pub-1", "boot", "https://api.outcomeci.com"),
                    client,
                )
        self.assertEqual(client.fallback_calls, [])
        self.assertEqual(len(client.failures), 1)


if __name__ == "__main__":
    unittest.main()


class MultiRunnerTests(unittest.TestCase):
    def test_every_leased_login_is_installed_and_codex_is_written_back(self):
        claim = {
            "content": "apiVersion: outcomeci.workflow/v1\n",
            "files": {},
            "trigger_name": "webhook",
            "input": {},
            "lease_token": "lease-secret",
            "agent": {"provider": "claude", "credential": "claude-token", "credential_version": 1},
            "agents": [
                {"provider": "claude", "credential": "claude-token", "credential_version": 1},
                {
                    "provider": "codex",
                    "credential": {"tokens": {"refresh_token": "rt-1"}},
                    "credential_version": 5,
                },
            ],
            "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
        }
        seen = {}

        class WorkflowClient:
            completed = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                pass

            def workflow_heartbeat(self, token, events=None):
                return {"active": True, "policy_events_received": len(events or [])}

            def workflow_complete(self, token, status, **values):
                self.completed.append((status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                options["on_created"]("run-1")
                seen["claude"] = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                seen["codex_home"] = os.environ.get("CODEX_HOME")
                (root / ".codex" / "auth.json").write_text('{"tokens": {"refresh_token": "rt-2"}}')
                outcome = root / ".outcomeci" / "outcomes" / "run-1"
                outcome.mkdir(parents=True)
                (outcome / "run.json").write_text("{}")
                return {"run_id": "run-1", "completed_steps": ["only"], "status": "completed"}

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch("outcomeci.cloud_runner.main.tempfile.mkdtemp", return_value=str(root)),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"only": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
            ):
                execute_workflow(
                    Launch("workflow", "invocation-1", "boot", "https://api.outcomeci.com"),
                    client,
                )

        self.assertEqual(seen["claude"], "claude-token")
        self.assertEqual(seen["codex_home"], str(root / ".codex"))
        status, values = client.completed[-1]
        self.assertEqual(status, "completed")
        self.assertEqual(values["expected_credential_version"], 5)
        self.assertEqual(values["agent_credential"], {"tokens": {"refresh_token": "rt-2"}})


class FinalReportTests(unittest.TestCase):
    """Once every step has run, nothing the runner reports may requeue the run."""

    claim = {
        "content": "apiVersion: outcomeci.workflow/v1\n",
        "files": {},
        "trigger_name": "webhook",
        "input": {},
        "lease_token": "lease-secret",
        "agent": {"provider": "claude", "credential": "claude-token", "credential_version": 1},
        "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {}},
    }

    def http_client(self, complete_statuses, heartbeat_status=200):
        """A real CoreClient over a mock transport; /complete answers in sequence."""
        completions = []
        statuses = iter(complete_statuses)

        def handler(request):
            suffix = request.url.path.rsplit("/", 1)[-1]
            if suffix == "claim":
                return httpx.Response(200, json=self.claim)
            if suffix == "start":
                return httpx.Response(204)
            if suffix == "heartbeat":
                return httpx.Response(
                    heartbeat_status, json={"active": True, "policy_events_received": 0}
                )
            if suffix == "complete":
                completions.append(json.loads(request.content))
                return httpx.Response(next(statuses), json={"completed": True})
            return httpx.Response(404)

        client = CoreClient(
            "https://api.outcomeci.com",
            "invocation-1",
            "boot",
            "workflow",
            transport=httpx.MockTransport(handler),
        )
        return client, completions

    def run_workflow(self, client, *, heartbeat_failure=False):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                options["on_created"]("run-1")
                outcome = root / ".outcomeci" / "outcomes" / "run-1"
                outcome.mkdir(parents=True)
                (outcome / "run.json").write_text("{}")
                return {"run_id": "run-1", "completed_steps": ["only"], "status": "completed"}

            class InlineThread:
                """Run the heartbeat loop once, synchronously, so its failure is recorded."""

                def __init__(self, target, daemon):
                    self.target = target

                def start(self):
                    self.target()

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch("outcomeci.cloud_runner.main.tempfile.mkdtemp", return_value=str(root)),
                mock.patch(
                    "outcomeci.cloud_runner.main.COMPLETION_REPORT_DELAYS_SECONDS", (0,) * 5
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.threading.Thread",
                    InlineThread if heartbeat_failure else threading.Thread,
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.HEARTBEAT_INTERVAL_SECONDS",
                    0 if heartbeat_failure else 15.0,
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"steps": {"only": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                try:
                    code = execute_workflow(
                        Launch("workflow", "invocation-1", "boot", "https://api.outcomeci.com"),
                        client,
                    )
                except Exception as error:  # noqa: BLE001 - returned for assertions
                    code = error
        events = [
            json.loads(line)
            for line in stderr.getvalue().splitlines()
            if "workflow_completion_report_failed" in line
        ]
        return code, events

    def test_a_transient_503_on_completion_is_retried_and_reported_once_completed(self):
        client, completions = self.http_client([503, 200])
        code, events = self.run_workflow(client)
        self.assertEqual(code, 0)
        self.assertEqual([item["status"] for item in completions], ["completed", "completed"])
        self.assertEqual(completions[0]["resource_usage"], completions[1]["resource_usage"])
        self.assertEqual(events, [])

    def test_persistent_5xx_on_completion_never_reports_a_retryable_failure(self):
        client, completions = self.http_client([503] * 6)
        code, events = self.run_workflow(client)
        self.assertEqual(code, 1)
        self.assertEqual([item["status"] for item in completions], ["completed"] * 6)
        self.assertFalse(any(item["retryable"] for item in completions))
        self.assertEqual(
            events,
            [
                {
                    "event": "workflow_completion_report_failed",
                    "report_category": "core_unavailable",
                    "status": "completed",
                    "requeued": False,
                }
            ],
        )

    def test_a_non_retryable_completion_rejection_still_reports_a_terminal_failure(self):
        client, completions = self.http_client([400, 200])
        code, events = self.run_workflow(client)
        self.assertIsInstance(code, CoreError)
        self.assertEqual(code.category, "core_rejected")
        self.assertFalse(code.retryable)
        self.assertEqual([item["status"] for item in completions], ["completed", "failed"])
        self.assertIs(completions[1]["retryable"], False)
        self.assertEqual(events, [])

    def test_a_failure_after_every_step_ran_is_reported_as_terminal(self):
        client, completions = self.http_client([503, 200], heartbeat_status=503)
        code, events = self.run_workflow(client, heartbeat_failure=True)
        self.assertIsInstance(code, CoreError)
        self.assertEqual(code.category, "policy_evidence_upload_failed")
        self.assertEqual([item["status"] for item in completions], ["failed", "failed"])
        self.assertFalse(any(item["retryable"] for item in completions))
        self.assertEqual(events, [])


def test_publication_worker_completes_real_validation_and_preserves_package_digest(
    tmp_path, monkeypatch
):
    from test_publication import sanitize_slack_package, slack_publication_source

    from outcomeci import publication

    source = slack_publication_source(tmp_path / "source")
    claim = publication_claim()
    claim["job"].update(content=source.read_text(), sensitive_terms=["outcomeci"])
    client = PublicationClient(claim)
    monkeypatch.setenv("AGENT_PRIVATE_ROOT", str(tmp_path))
    monkeypatch.setattr(publication, "invoke", sanitize_slack_package)
    assert (
        execute_publication(
            Launch("publication", "pub-1", "boot", "https://api.outcomeci.com"), client
        )
        == 0
    )
    assert client.failures == []
    token, payload = client.completions[0]
    assert token == "completion-secret"
    assert "trigger.channel" in payload["content"]
    package = {
        "outcome.yml": payload["content"],
        **{path: base64.b64decode(value).decode() for path, value in payload["files"].items()},
    }
    digest = hashlib.sha256()
    for path, content in sorted(package.items()):
        digest.update(path.encode() + b"\0" + content.encode() + b"\0")
    assert payload["package_sha256"] == digest.hexdigest()


def test_publication_failure_category_does_not_contain_private_diagnostics():
    from outcomeci.publication import PublicationValidationError

    failure = PublicationValidationError(
        "publication_privacy_failed", "private/path: confidential value"
    )
    assert cloud_runner_main.workflow_failure_category(failure) == "publication_privacy_failed"
