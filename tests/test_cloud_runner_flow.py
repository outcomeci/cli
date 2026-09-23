import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from outcomeci.cloud_runner.client import CoreError
from outcomeci.cloud_runner.main import (
    _is_usage_limit_error,
    authorize,
    execute,
    execute_publication,
    execute_workflow,
    workflow_failure_category,
)
from outcomeci.cloud_runner.models import AuthorizationClaim, ContractError, ExecutionClaim, Launch
from outcomeci.cloud_runner.process import ProcessResult
from outcomeci.process import ExecutionError


def outcome_claim(provider: str = "codex") -> ExecutionClaim:
    hydration = {
        "provider": provider,
        "auth_json": {"auth_mode": "chatgpt", "refresh_token": "secret"},
    }
    if provider == "claude":
        hydration = {"provider": provider, "oauth_token": "claude-secret"}
    elif provider == "opencode":
        hydration = {"provider": provider, "api_key": "openrouter-secret"}
    return ExecutionClaim.parse(
        {
            "job": {
                "job_id": "job_1",
                "kind": "outcome",
                "repositories": ["owner/state", "owner/product"],
                "agent": provider,
                "model": (
                    "openrouter/anthropic/claude-sonnet-4" if provider == "opencode" else None
                ),
            },
            "lease_id": "lease_1",
            "credential_version": 3,
            "lease_expires_at": "2026-08-20T00:00:00Z",
            "completion_token": "completion",
            "core_job_token": "job-token",
            "command": ["oci", "outcome", "run"],
            "github_token": "github-token",
            "hydration": hydration,
            "timeout_seconds": 60,
            "outcome": {"phase": "plan"},
        }
    )


class FakeClient:
    def __init__(self, claim: ExecutionClaim):
        self.claim = claim
        self.completions: list[tuple] = []
        self.failures: list[tuple] = []
        self.heartbeats: list[tuple] = []
        self.logs: list[tuple] = []

    def claim_execution(self):
        return self.claim

    def heartbeat(self, *args):
        self.heartbeats.append(args)
        return {"lease_expires_at": "2026-08-20T01:00:00Z", "phase": args[2]}

    def complete(self, *args):
        self.completions.append(args)

    def fail(self, *args):
        self.failures.append(args)

    def log(self, *args):
        self.logs.append(args)


class FlowTests(unittest.TestCase):
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
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
                        "instructions": {"phases": {"resolve_analytics": {}}},
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

    def test_execute_claim_conflict_is_also_a_clean_no_op(self):
        class ClaimConflictClient:
            def claim_execution(self):
                raise CoreError("lease_conflict", True)

        result = execute(
            Launch("execute", "invocation-1", "boot", "https://api.outcomeci.com"),
            ClaimConflictClient(),
        )

        self.assertEqual(result, 0)

    def test_execute_non_retryable_claim_failure_still_raises(self):
        class ClaimRejectedClient:
            def claim_execution(self):
                raise CoreError("claim_rejected", False)

        with self.assertRaises(CoreError):
            execute(
                Launch("execute", "invocation-1", "boot", "https://api.outcomeci.com"),
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
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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

            def trigger(workspace, config, name, payload, **options):
                self.assertEqual(options["execution_backend"], "outcomeci")
                self.assertIs(options["_container_isolated"], True)
                self.assertEqual(
                    options["credential_resolver"]("vault:slack/bot-token")["secrets"]["value"],
                    "slack-secret",
                )
                self.assertEqual(name, "inbound")
                options["on_created"]("run-1")
                trace = workspace / ".outcomeci" / "outcomes" / "run-1" / "transcripts"
                trace.mkdir(parents=True)
                (trace / "codex.jsonl").write_text('{"type":"event"}\n')
                (workspace / ".outcomeci" / "outcomes" / "run-1" / ".env").write_text(
                    "TOKEN=never-upload\n"
                )
                options["event_sink"](
                    {
                        "event_id": "00000000-0000-0000-0000-000000000001",
                        "occurred_at": "2026-09-16T00:00:00+00:00",
                        "event_type": "permission.reviewed",
                        "phase": "notify",
                        "capability": "slack.request",
                        "message": "Permission advisor: allow",
                        "decision": "allow",
                    }
                )
                return {
                    "run_id": "run-1",
                    "status": "completed",
                    "completed_phases": ["notify"],
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
                        "instructions": {"phases": {"notify": {}}},
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
        self.assertEqual(client.heartbeats[0][1][0]["event_type"], "permission.reviewed")

    def test_generic_workflow_auto_continues_through_ready_phases(self):
        claim = {
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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

            def trigger(workspace, config, name, payload, **options):
                self.assertEqual(options["execution_backend"], "outcomeci")
                options["on_created"]("run-1")
                return {
                    "run_id": "run-1",
                    "status": "awaiting_confirmation",
                    "completed_phases": ["resolve_analytics"],
                    "ready_phases": ["notify"],
                }

            def continue_run(root_arg, config_arg, run_id, *, approve, **options):
                continue_calls.append((run_id, approve, options))
                self.assertEqual(options["execution_backend"], "outcomeci")
                self.assertIs(options["_container_isolated"], True)
                self.assertTrue(callable(options["credential_resolver"]))
                trace = root_arg / ".outcomeci" / "outcomes" / run_id / "transcripts"
                trace.mkdir(parents=True, exist_ok=True)
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "completed_phases": ["resolve_analytics", "notify"],
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
                        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
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
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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

            def trigger(workspace, config, name, payload, **options):
                trigger_calls.append(options)
                options["on_created"]("run-1")
                raise ExecutionError("codex failed with exit 1: Usage limit reached, try later")

            def retry(root_arg, config_arg, run_id, **options):
                retry_calls.append(options)
                self.assertEqual(options["agent"], "claude")
                self.assertEqual(options["model"], "claude-opus-5")
                return {
                    "run_id": run_id,
                    "status": "awaiting_confirmation",
                    "completed_phases": ["resolve_analytics"],
                    "ready_phases": ["notify"],
                }

            def continue_run(root_arg, config_arg, run_id, *, approve, **options):
                continue_calls.append(options)
                self.assertEqual(options["agent"], "claude")
                trace = root_arg / ".outcomeci" / "outcomes" / run_id / "transcripts"
                trace.mkdir(parents=True, exist_ok=True)
                return {
                    "run_id": run_id,
                    "status": "completed",
                    "completed_phases": ["resolve_analytics", "notify"],
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
                        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
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
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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

            def trigger(workspace, config, name, payload, **options):
                options["on_created"]("run-1")
                raise ExecutionError("codex failed with exit 1: Usage limit reached, try later")

            def retry(root_arg, config_arg, run_id, **options):
                self.assertEqual(options["agent"], "claude")
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
                        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
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

    def test_a_rejected_completion_report_is_captured_instead_of_silently_swallowed(self):
        claim = {
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
                        "instructions": {"phases": {"resolve_analytics": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
                mock.patch("outcomeci.cloud_runner.main.capture_exception") as capture,
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
        capture.assert_called_once()
        (reported_error,), kwargs = capture.call_args
        self.assertIsInstance(reported_error, CoreError)
        self.assertEqual(reported_error.category, "core_conflict")
        self.assertEqual(kwargs["event"], "workflow_completion_report_failed")
        self.assertEqual(kwargs["original_category"], "agent_process_failed")

    def test_non_usage_limit_failure_never_triggers_the_fallback(self):
        claim = {
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
                        "instructions": {"phases": {"notify": {}}},
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

    def test_generic_workflow_reports_awaiting_input_and_returns_zero(self):
        claim = {
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
                (trace / "codex.jsonl").write_text('{"type":"event"}\n')
                return {
                    "run_id": "run-1",
                    "status": "awaiting_input",
                    "completed_phases": [],
                    "ready_phases": [],
                    "pending_interaction": {
                        "phase": "plan",
                        "timing": "before",
                        "id": "approval-1",
                        "path": str(workspace / "interaction.json"),
                    },
                }

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.local.continue_run",
                    side_effect=AssertionError("continue_run should not be called"),
                ),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"phases": {"resolve_analytics": {}, "notify": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
            ):
                result = execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )
        self.assertEqual(result, 0)
        self.assertEqual(client.completed[0][0:2], ("lease-secret", "awaiting_input"))
        values = client.completed[0][2]
        self.assertEqual(values["run_id"], "run-1")
        self.assertEqual(
            values["pending_interaction"],
            {"phase": "plan", "timing": "before", "id": "approval-1"},
        )
        self.assertEqual(
            [item["path"] for item in values["artifacts"]],
            [".outcomeci/outcomes/run-1/transcripts/codex.jsonl"],
        )

    def test_generic_workflow_resumes_from_a_restored_artifact_bundle(self):
        claim = {
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
            "resume": {
                "run_id": "run-1",
                "interaction_id": "approval-1",
                "message": "looks good",
                "approve": True,
                "reject": False,
            },
        }
        restored_content = b'{"schema_version":1,"run_id":"run-1"}'

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

            def workflow_restore_artifacts(self, token):
                return [
                    {
                        "path": ".outcomeci/outcomes/run-1/run.json",
                        "content_base64": base64.b64encode(restored_content).decode(),
                        "sha256": "unused-in-test",
                    }
                ]

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        respond_calls = []

        def respond(workspace, config, run_id, interaction_id, message, **options):
            respond_calls.append((run_id, interaction_id, message, options))
            self.assertEqual(
                (workspace / ".outcomeci" / "outcomes" / "run-1" / "run.json").read_bytes(),
                restored_content,
            )
            return {
                "run_id": "run-1",
                "status": "completed",
                "completed_phases": ["notify"],
            }

        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.local.respond", side_effect=respond),
                mock.patch(
                    "outcomeci.local.trigger",
                    side_effect=AssertionError("trigger should not be called on resume"),
                ),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"phases": {"notify": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
            ):
                result = execute_workflow(
                    Launch(
                        "workflow",
                        "invocation-1",
                        "boot",
                        "https://api.outcomeci.com",
                    ),
                    client,
                )
        self.assertEqual(result, 0)
        self.assertEqual(len(respond_calls), 1)
        run_id, interaction_id, message, options = respond_calls[0]
        self.assertEqual((run_id, interaction_id, message), ("run-1", "approval-1", "looks good"))
        self.assertTrue(options["approve"])
        self.assertFalse(options["reject"])
        self.assertEqual(options["execution_backend"], "outcomeci")
        self.assertIs(options["_container_isolated"], True)
        self.assertEqual(client.completed[0][0:2], ("lease-secret", "completed"))
        self.assertEqual(client.completed[0][2]["run_id"], "run-1")

    def test_generic_workflow_rejects_a_restored_artifact_that_escapes_its_root(self):
        claim = {
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
            "resume": {
                "run_id": "run-1",
                "interaction_id": "approval-1",
                "message": "looks good",
                "approve": True,
                "reject": False,
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

            def workflow_restore_artifacts(self, token):
                return [
                    {
                        "path": "../escape.txt",
                        "content_base64": base64.b64encode(b"nope").decode(),
                        "sha256": "unused-in-test",
                    }
                ]

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
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
                    "outcomeci.local.respond",
                    side_effect=AssertionError("respond should not be called"),
                ),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={
                        "instructions": {"phases": {"notify": {}}},
                        "workflow": {"spec": {"agents": {"default": {}}}},
                    },
                ),
                self.assertRaisesRegex(ContractError, "escaped its root"),
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
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
                        "instructions": {"phases": {"notify": {}}},
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

    def test_outcome_executes_with_scoped_tokens_and_persists_result(self):
        claim = outcome_claim("codex")
        client = FakeClient(claim)
        result = {
            "status": "awaiting_confirmation",
            "manifest": {},
            "artifact_paths": [],
        }
        with tempfile.TemporaryDirectory() as parent:
            root, workspace = Path(parent) / "private", Path(parent) / "workspace"
            root.mkdir()
            workspace.mkdir()

            def fake_run(command, *, cwd, env, timeout, on_tick):
                self.assertEqual(cwd, workspace)
                self.assertEqual(env["GITHUB_TOKEN"], "github-token")
                self.assertEqual(env["OUTCOMECI_API_KEY"], "job-token")
                self.assertEqual(
                    json.loads((workspace / "outcome-claim.json").read_text()),
                    claim.outcome,
                )
                on_tick()
                return ProcessResult(0, json.dumps(result), "")

            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENT_PRIVATE_ROOT": parent, "AGENT_WORK_ROOT": str(workspace)},
                    clear=False,
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=fake_run),
            ):
                self.assertEqual(
                    execute(
                        Launch("outcome", "job_1", "boot", "https://api.outcomeci.com"),
                        client,
                    ),
                    0,
                )
            self.assertFalse(root.exists())
        self.assertEqual(client.completions, [("completion", {"result": result})])
        self.assertEqual(client.heartbeats[0][2], "preparing")
        self.assertEqual(
            [entry[1]["event_type"] for entry in client.logs],
            ["runner.claimed", "agent.started", "agent.completed"],
        )
        self.assertEqual([entry[1]["sequence"] for entry in client.logs], [1, 2, 3])
        self.assertTrue(all(entry[1]["phase"] == "plan" for entry in client.logs))
        self.assertNotIn("github-token", json.dumps(client.logs))
        self.assertNotIn("job-token", json.dumps(client.logs))

    def test_heartbeat_continues_while_agent_is_silent(self):
        claim = outcome_claim("codex")
        client = FakeClient(claim)
        result = {"status": "completed", "manifest": {}, "artifact_paths": []}
        with tempfile.TemporaryDirectory() as parent:
            root, workspace = Path(parent) / "private", Path(parent) / "workspace"
            root.mkdir()
            workspace.mkdir()

            def silent_run(command, *, cwd, env, timeout, on_tick):
                time.sleep(0.04)
                return ProcessResult(0, json.dumps(result), "")

            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENT_PRIVATE_ROOT": parent, "AGENT_WORK_ROOT": str(workspace)},
                    clear=False,
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.cloud_runner.main.HEARTBEAT_INTERVAL_SECONDS", 0.01),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=silent_run),
            ):
                self.assertEqual(
                    execute(
                        Launch("outcome", "job_1", "boot", "https://api.outcomeci.com"),
                        client,
                    ),
                    0,
                )
        self.assertGreaterEqual(len(client.heartbeats), 3)
        self.assertEqual(len(client.completions), 1)

    def test_opencode_hydrates_openrouter_key_in_private_home(self):
        claim = outcome_claim("opencode")
        client = FakeClient(claim)
        result = {"status": "completed", "manifest": {}, "artifact_paths": []}
        with tempfile.TemporaryDirectory() as parent:
            root, workspace = Path(parent) / "private", Path(parent) / "workspace"
            root.mkdir()
            workspace.mkdir()

            def fake_run(command, *, cwd, env, timeout, on_tick):
                self.assertEqual(env["OPENROUTER_API_KEY"], "openrouter-secret")
                self.assertEqual(env["HOME"], str(root / "opencode"))
                return ProcessResult(0, json.dumps(result), "")

            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENT_PRIVATE_ROOT": parent, "AGENT_WORK_ROOT": str(workspace)},
                    clear=False,
                ),
                mock.patch(
                    "outcomeci.cloud_runner.main.tempfile.mkdtemp",
                    return_value=str(root),
                ),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=fake_run),
            ):
                self.assertEqual(
                    execute(
                        Launch("outcome", "job_1", "boot", "https://api.outcomeci.com"),
                        client,
                    ),
                    0,
                )
            self.assertFalse(root.exists())

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
            "content": "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n",
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
    (destination / "outcome.yml").write_text(
        "apiVersion: outcomeci.dev/v1alpha1\nkind: OutcomeWorkflow\n"
    )
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
