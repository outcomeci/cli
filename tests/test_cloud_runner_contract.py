import json
import tempfile
import unittest
from pathlib import Path

import httpx

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
    def test_runner_accepts_only_known_modes(self):
        for mode in ("authorize", "outcome", "workflow", "publication"):
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

    def test_client_posts_json_and_returns_the_parsed_body(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["authorization"]
            return httpx.Response(200, json={"lease_token": "lease-1"})

        client = CoreClient(
            "https://api.outcomeci.com",
            "job",
            "bootstrap",
            "workflow",
            transport=httpx.MockTransport(handler),
        )
        result = client.claim_workflow()
        self.assertEqual(result, {"lease_token": "lease-1"})
        self.assertEqual(captured["authorization"], "Bearer bootstrap")
        self.assertTrue(captured["url"].endswith("/v1/internal/workflow-invocations/job/claim"))

    def test_client_rejects_an_oversized_response(self):
        oversized = json.dumps({"padding": "x" * (1024 * 1024)})
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=oversized))
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "outcome", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "invalid_core_response")

    def test_client_treats_401_as_claim_rejected(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="unauthorized"))
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "outcome", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "claim_rejected")

    def test_client_treats_a_network_failure_as_retryable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = CoreClient(
            "https://api.outcomeci.com",
            "job",
            "bootstrap",
            "outcome",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "core_unavailable")
        self.assertTrue(raised.exception.retryable)

    def test_client_uses_outcome_job_endpoint_and_classifies_conflicts(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(409, text="error"))
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "outcome", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_execution()
        self.assertEqual(raised.exception.category, "core_conflict")
        self.assertTrue(raised.exception.retryable)

    def test_client_preserves_effect_evidence_conflicts(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                409,
                json={
                    "detail": "required integration effect evidence is missing: "
                    "notify:slack.request"
                },
            )
        )
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "workflow", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "workflow_effect_evidence_missing")
        self.assertFalse(raised.exception.retryable)

    def test_client_treats_lease_and_connection_conflicts_as_retryable(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                409,
                json={"detail": "configured agent connection is already running another workflow"},
            )
        )
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "workflow", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "core_conflict")
        self.assertTrue(raised.exception.retryable)

        lease_transport = httpx.MockTransport(
            lambda request: httpx.Response(409, json={"detail": "workflow is not awaiting start"})
        )
        lease_client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "workflow", transport=lease_transport
        )
        with self.assertRaises(CoreError) as raised:
            lease_client.claim_workflow()
        self.assertEqual(raised.exception.category, "lease_conflict")
        self.assertTrue(raised.exception.retryable)

    def test_client_treats_credential_and_policy_conflicts_as_non_retryable(self):
        for detail, category in (
            ("agent credential changed during workflow", "credential_conflict"),
            ("policy review digest mismatch", "policy_review_conflict"),
        ):
            transport = httpx.MockTransport(
                lambda request, detail=detail: httpx.Response(409, json={"detail": detail})
            )
            client = CoreClient(
                "https://api.outcomeci.com", "job", "bootstrap", "workflow", transport=transport
            )
            with self.assertRaises(CoreError) as raised:
                client.claim_workflow()
            self.assertEqual(raised.exception.category, category)
            self.assertFalse(raised.exception.retryable)

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
