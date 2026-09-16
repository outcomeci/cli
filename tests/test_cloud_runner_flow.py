import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from outcomeci.cloud_runner.main import execute, execute_workflow
from outcomeci.cloud_runner.models import ExecutionClaim, Launch
from outcomeci.cloud_runner.process import ProcessResult


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
                "model": "openrouter/anthropic/claude-sonnet-4" if provider == "opencode" else None,
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
                        "configuration": {"header_name": "Authorization", "scheme": "Bearer"},
                        "secrets": {"value": "slack-secret"},
                    }
                },
            },
        }

        class WorkflowClient:
            completed = []

            def claim_workflow(self):
                return claim

            def workflow_start(self, token):
                self.started = token

            def workflow_heartbeat(self, token):
                pass

            def workflow_complete(self, token, status, **values):
                self.completed.append((token, status, values))

        client = WorkflowClient()
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "private"
            root.mkdir()

            def trigger(workspace, config, name, payload, **options):
                self.assertEqual(options["execution_backend"], "outcomeci")
                self.assertEqual(
                    options["credential_resolver"]("vault:slack/bot-token")["secrets"]["value"],
                    "slack-secret",
                )
                self.assertEqual(name, "inbound")
                return {
                    "run_id": "run-1",
                    "status": "completed",
                    "completed_phases": ["notify"],
                }

            with (
                mock.patch.dict(os.environ, {"AGENT_PRIVATE_ROOT": parent}, clear=False),
                mock.patch("outcomeci.cloud_runner.main.tempfile.mkdtemp", return_value=str(root)),
                mock.patch("outcomeci.local.trigger", side_effect=trigger),
                mock.patch(
                    "outcomeci.config.compile_workflow",
                    return_value={"instructions": {"phases": {"notify": {}}}},
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
        self.assertEqual(client.completed[0][2]["run_id"], "run-1")
        self.assertEqual(client.completed[0][2]["expected_credential_version"], 3)
        self.assertEqual(client.completed[0][2]["agent_credential"], {"token": "agent-secret"})

    def test_outcome_executes_with_scoped_tokens_and_persists_result(self):
        claim = outcome_claim("codex")
        client = FakeClient(claim)
        result = {"status": "awaiting_confirmation", "manifest": {}, "artifact_paths": []}
        with tempfile.TemporaryDirectory() as parent:
            root, workspace = Path(parent) / "private", Path(parent) / "workspace"
            root.mkdir()
            workspace.mkdir()

            def fake_run(command, *, cwd, env, timeout, on_tick):
                self.assertEqual(cwd, workspace)
                self.assertEqual(env["GITHUB_TOKEN"], "github-token")
                self.assertEqual(env["OUTCOMECI_API_KEY"], "job-token")
                self.assertEqual(
                    json.loads((workspace / "outcome-claim.json").read_text()), claim.outcome
                )
                on_tick()
                return ProcessResult(0, json.dumps(result), "")

            with (
                mock.patch.dict(
                    os.environ,
                    {"AGENT_PRIVATE_ROOT": parent, "AGENT_WORK_ROOT": str(workspace)},
                    clear=False,
                ),
                mock.patch("outcomeci.cloud_runner.main.tempfile.mkdtemp", return_value=str(root)),
                mock.patch("outcomeci.cloud_runner.main.run", side_effect=fake_run),
            ):
                self.assertEqual(
                    execute(
                        Launch("outcome", "job_1", "boot", "https://api.outcomeci.com"), client
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
                mock.patch("outcomeci.cloud_runner.main.tempfile.mkdtemp", return_value=str(root)),
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


if __name__ == "__main__":
    unittest.main()
