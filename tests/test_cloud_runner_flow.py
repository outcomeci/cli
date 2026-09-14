import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from outcomeci.cloud_runner.main import execute
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

    def claim_execution(self):
        return self.claim

    def heartbeat(self, *args):
        self.heartbeats.append(args)
        return {"lease_expires_at": "2026-08-20T01:00:00Z", "phase": args[2]}

    def complete(self, *args):
        self.completions.append(args)

    def fail(self, *args):
        self.failures.append(args)


class FlowTests(unittest.TestCase):
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
