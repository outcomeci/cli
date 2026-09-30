import json
import tempfile
import unittest
from pathlib import Path

import httpx

from outcomeci.cloud_runner.client import CoreClient, CoreError
from outcomeci.cloud_runner.main import classify_failure, safe_env, safe_verification
from outcomeci.cloud_runner.models import ContractError, Launch


class ContractTests(unittest.TestCase):
    def test_runner_accepts_only_known_modes(self):
        for mode in ("authorize", "workflow", "publication"):
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
                    "AGENT_RUNNER_MODE": "outcome",
                    "AGENT_JOB_ID": "job",
                    "AGENT_BOOTSTRAP_TOKEN": "opaque",
                    "OUTCOMECI_API_URL": "https://api.outcomeci.com",
                }
            )

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
            "https://api.outcomeci.com", "job", "bootstrap", "authorize", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "invalid_core_response")

    def test_client_includes_resource_usage_in_workflow_completion(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(204)

        client = CoreClient(
            "https://api.outcomeci.com",
            "job",
            "bootstrap",
            "workflow",
            transport=httpx.MockTransport(handler),
        )
        usage = {
            "schema_version": 1,
            "sample_count": 2,
            "sampled_milliseconds": 1000,
            "memory_peak_bytes": 1024,
        }

        client.workflow_complete("lease", "completed", resource_usage=usage)

        self.assertEqual(captured["resource_usage"], usage)

    def test_client_treats_401_as_claim_rejected(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="unauthorized"))
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "workflow", transport=transport
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
            "workflow",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
        self.assertEqual(raised.exception.category, "core_unavailable")
        self.assertTrue(raised.exception.retryable)

    def test_client_classifies_conflicts_as_retryable(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(409, text="error"))
        client = CoreClient(
            "https://api.outcomeci.com", "job", "bootstrap", "workflow", transport=transport
        )
        with self.assertRaises(CoreError) as raised:
            client.claim_workflow()
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
