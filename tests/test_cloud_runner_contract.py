import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from outcomeci.cloud_runner.client import CoreClient, CoreError
from outcomeci.cloud_runner.main import classify_failure, safe_env, safe_verification
from outcomeci.cloud_runner.models import ContractError, ExecutionClaim, Launch


def outcome_claim(provider: str = "codex") -> ExecutionClaim:
    hydration = {"provider": provider}
    hydration["auth_json" if provider == "codex" else "oauth_token"] = (
        {"auth_mode": "chatgpt", "refresh_token": "secret"}
        if provider == "codex"
        else "claude-secret"
    )
    return ExecutionClaim.parse(
        {
            "job": {
                "job_id": "job_1",
                "kind": "outcome",
                "repositories": ["owner/state", "owner/product"],
                "agent": provider,
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


class ContractTests(unittest.TestCase):
    def test_runner_accepts_only_authorization_and_outcome_modes(self):
        for mode in ("authorize", "outcome"):
            launch = Launch.from_env(
                {
                    "AGENT_RUNNER_MODE": mode,
                    "AGENT_JOB_ID": "job",
                    "AGENT_BOOTSTRAP_TOKEN": "opaque",
                    "OUTCOMECI_API_URL": "https://api.outcomeci.com",
                }
            )
            self.assertEqual(launch.mode, mode)
        with self.assertRaises(ContractError):
            Launch.from_env(
                {
                    "AGENT_RUNNER_MODE": "execute",
                    "AGENT_JOB_ID": "job",
                    "AGENT_BOOTSTRAP_TOKEN": "opaque",
                    "OUTCOMECI_API_URL": "https://api.outcomeci.com",
                }
            )

    def test_outcome_claim_requires_outcome_binding(self):
        self.assertEqual(outcome_claim().outcome, {"phase": "plan"})
        raw = {
            "job": {
                "job_id": "job",
                "kind": "outcome",
                "repositories": ["owner/repo"],
                "agent": "codex",
            },
            "lease_id": "lease",
            "credential_version": 1,
            "lease_expires_at": "soon",
            "completion_token": "complete",
            "core_job_token": "job-token",
            "command": ["oci", "outcome", "run"],
            "github_token": "github",
            "hydration": {"provider": "codex", "auth_json": {}},
        }
        with self.assertRaisesRegex(ContractError, "outcome binding"):
            ExecutionClaim.parse(raw)

    def test_client_uses_outcome_job_endpoint_and_classifies_conflicts(self):
        client = CoreClient("https://api.outcomeci.com", "job", "bootstrap", "outcome")
        error = urllib.error.HTTPError("https://api.outcomeci.com", 409, "error", None, None)
        with (
            mock.patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(CoreError) as raised,
        ):
            client.claim_execution()
        self.assertEqual(raised.exception.category, "lease_conflict")

    def test_safe_helpers_preserve_scoped_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            env = safe_env(Path(directory), "github-secret", "job-secret")
        self.assertEqual(env["GITHUB_TOKEN"], "github-secret")
        self.assertEqual(env["OUTCOMECI_API_KEY"], "job-secret")
        self.assertIsNone(safe_verification("codex", "Open https://auth.openai.com/device"))
        self.assertEqual(
            safe_verification("codex", "Open https://auth.openai.com/device code ABCD-EFGH"),
            ("https://auth.openai.com/device", "ABCD-EFGH"),
        )
        self.assertEqual(
            classify_failure("Failed to refresh token", authorization=False),
            ("provider_auth_rejected", False),
        )


if __name__ == "__main__":
    unittest.main()
