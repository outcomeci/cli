"""Authenticated client for one-use Core worker claims."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any

from .models import AuthorizationClaim, ExecutionClaim


class CoreError(RuntimeError):
    def __init__(self, category: str, retryable: bool = False):
        super().__init__(category)
        self.category, self.retryable = category, retryable


class CoreClient:
    def __init__(
        self,
        base_url: str,
        object_id: str,
        bootstrap_token: str,
        mode: str,
        timeout: int = 20,
    ):
        resource = (
            "agent-auth-attempts"
            if mode == "authorize"
            else "workflow-invocations"
            if mode == "workflow"
            else "outcome-jobs"
        )
        self._url = f"{base_url}/v1/internal/{resource}/{object_id}"
        self._base_url = base_url
        self._token = bootstrap_token
        self._timeout = timeout
        self._max_response = 24 * 1024 * 1024 if mode == "workflow" else 1024 * 1024

    def _post(
        self, suffix: str, payload: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._url}/{suffix}",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {token or self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                if response.status == 204:
                    return {}
                raw = response.read(self._max_response + 1)
                if len(raw) > self._max_response:
                    raise CoreError("invalid_core_response")
                result = json.loads(raw or b"{}")
                if not isinstance(result, dict):
                    raise CoreError("invalid_core_response")
                return result
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise CoreError("claim_rejected") from error
            if error.code == 409:
                category = "core_conflict"
                with suppress(Exception):
                    body = json.loads(error.read(4097))
                    detail = str(body.get("detail", "")).casefold()
                    if "lease" in detail or "workflow is not" in detail:
                        category = "lease_conflict"
                    elif "effect evidence" in detail:
                        category = "workflow_effect_evidence_missing"
                    elif "credential changed" in detail:
                        category = "credential_conflict"
                    elif "policy review digest" in detail:
                        category = "policy_review_conflict"
                raise CoreError(category) from error
            raise CoreError("core_unavailable", error.code >= 500) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise CoreError("core_unavailable", True) from error

    def claim_authorization(self) -> AuthorizationClaim:
        return AuthorizationClaim.parse(self._post("claim", {}))

    def claim_execution(self) -> ExecutionClaim:
        return ExecutionClaim.parse(self._post("claim", {}))

    def claim_workflow(self) -> dict[str, Any]:
        return self._post("claim", {})

    def workflow_start(self, lease_token: str) -> None:
        self._post("start", {"lease_token": lease_token})

    def workflow_heartbeat(
        self, lease_token: str, events: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return self._post("heartbeat", {"lease_token": lease_token, "events": events or []})

    def workflow_policy_review(self, lease_token: str, proposal: dict[str, Any]) -> dict[str, Any]:
        return self._post("policy-review", {"lease_token": lease_token, "proposal": proposal})

    def workflow_credential(self, lease_token: str, reference: str) -> Any:
        return self._post(
            "credentials/resolve",
            {"lease_token": lease_token, "reference": reference},
        ).get("value")

    def workflow_complete(
        self,
        lease_token: str,
        status: str,
        *,
        run_id: str | None = None,
        artifacts: list[dict[str, str]] | None = None,
        category: str | None = None,
        expected_credential_version: int | None = None,
        agent_credential: Any | None = None,
    ) -> None:
        self._post(
            "complete",
            {
                "lease_token": lease_token,
                "status": status,
                "run_id": run_id,
                "artifacts": artifacts or [],
                "category": category,
                "expected_credential_version": expected_credential_version,
                "agent_credential": agent_credential,
            },
        )

    def verification(
        self, session_token: str, url: str, code: str | None, expires_at: str | None
    ) -> None:
        self._post(
            "verification",
            {"verification_url": url, "user_code": code, "expires_at": expires_at},
            session_token,
        )

    def authorization_response(self, session_token: str) -> str | None:
        result = self._post("response", {}, session_token)
        code = result.get("code")
        if code is None:
            return None
        if not isinstance(code, str) or not code:
            raise CoreError("invalid_core_response")
        return code

    def complete(self, session_token: str, payload: dict[str, Any]) -> None:
        self._post("complete", payload, session_token)

    def heartbeat(
        self, session_token: str, lease_id: str, phase: str, detail: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"lease_id": lease_id, "phase": phase}
        if detail:
            payload["detail"] = detail
        result = self._post("heartbeat", payload, session_token)
        if not isinstance(result.get("lease_expires_at"), str) or result.get("phase") != phase:
            raise CoreError("invalid_core_response")
        return result

    def log(self, session_token: str, payload: dict[str, Any]) -> None:
        self._post("logs", payload, session_token)

    def fail(
        self,
        session_token: str,
        category: str,
        retryable: bool,
        lease_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"category": category, "retryable": retryable}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        self._post("fail", payload, session_token)
