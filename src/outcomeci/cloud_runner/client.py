"""Authenticated client for one-use Core worker claims."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

import httpx

from .models import AuthorizationClaim
from .resource_usage import ResourceUsageReport


class CoreError(RuntimeError):
    def __init__(self, category: str, retryable: bool = False, detail: str | None = None):
        # The detail is what the run's log shows about why, such as the api's
        # reason for refusing a request.
        super().__init__(f"{category}: {detail}" if detail else category)
        self.category, self.retryable = category, retryable


def _refusal(suffix: str, response: httpx.Response) -> str:
    """Which call OutcomeCI Cloud refused and its stated reason. A validation
    error keeps each field's location and message, never the input it echoes."""
    reason = ""
    with suppress(Exception):
        detail = json.loads(_read_bounded(response, 4097)).get("detail")
        if isinstance(detail, list):
            reason = "; ".join(
                f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg', '')}"
                for item in detail[:5]
                if isinstance(item, dict)
            )
        elif isinstance(detail, str):
            reason = detail
    return f"{suffix} returned HTTP {response.status_code}" + (
        f": {reason[:500]}" if reason else ""
    )


MODEL_TURN_TIMEOUT_SECONDS = 90


def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    """Read at most `limit` bytes of a response body, like file.read(limit)
    -- never buffer more, regardless of what Content-Length claims. A
    broken or malicious Core deployment must not be able to exhaust memory
    here."""
    raw = bytearray()
    for chunk in response.iter_bytes():
        raw.extend(chunk)
        if len(raw) >= limit:
            break
    return bytes(raw[:limit])


class CoreClient:
    def __init__(
        self,
        base_url: str,
        object_id: str,
        bootstrap_token: str,
        mode: str,
        timeout: int = 20,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        resource = (
            "agent-auth-attempts"
            if mode == "authorize"
            else "workflow-invocations"
            if mode == "workflow"
            else "publication-jobs"
        )
        self._url = f"{base_url}/v1/internal/{resource}/{object_id}"
        self._base_url = base_url
        self._token = bootstrap_token
        self._timeout = timeout
        self._transport = transport
        self._max_response = (
            24 * 1024 * 1024 if mode in {"workflow", "publication"} else 1024 * 1024
        )

    def _post(
        self,
        suffix: str,
        payload: dict[str, Any],
        token: str | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token or self._token}"}
        try:
            with (
                httpx.Client(transport=self._transport, timeout=timeout or self._timeout) as client,
                client.stream(
                    "POST", f"{self._url}/{suffix}", json=payload, headers=headers
                ) as response,
            ):
                if response.status_code == 204:
                    return {}
                if response.status_code in (401, 403):
                    raise CoreError("claim_rejected")
                if response.status_code == 409:
                    # lease_conflict/core_conflict are contention with another
                    # concurrently running invocation (e.g. an agent connection
                    # already in use) -- transient, and worth retrying. The other
                    # 409 categories are real data/state problems a bare retry
                    # won't fix, so they stay non-retryable.
                    category, retryable = "core_conflict", True
                    with suppress(Exception):
                        body = json.loads(_read_bounded(response, 4097))
                        detail = str(body.get("detail", "")).casefold()
                        if "lease" in detail or "workflow is not" in detail:
                            category = "lease_conflict"
                        elif "effect evidence" in detail:
                            category, retryable = "workflow_effect_evidence_missing", False
                        elif "credential changed" in detail:
                            category, retryable = "credential_conflict", False
                        elif "policy review digest" in detail:
                            category, retryable = "policy_review_conflict", False
                    raise CoreError(category, retryable)
                if response.status_code >= 400:
                    raise CoreError(
                        "core_unavailable",
                        response.status_code >= 500,
                        _refusal(suffix, response),
                    )
                raw = _read_bounded(response, self._max_response + 1)
        except httpx.HTTPError as error:
            raise CoreError("core_unavailable", True) from error
        if len(raw) > self._max_response:
            raise CoreError("invalid_core_response")
        try:
            result = json.loads(raw or b"{}")
        except json.JSONDecodeError as error:
            raise CoreError("core_unavailable", True) from error
        if not isinstance(result, dict):
            raise CoreError("invalid_core_response")
        return result

    def claim_authorization(self) -> AuthorizationClaim:
        return AuthorizationClaim.parse(self._post("claim", {}))

    def claim_workflow(self) -> dict[str, Any]:
        return self._post("claim", {})

    def claim_publication(self) -> dict[str, Any]:
        return self._post("claim", {})

    def complete_publication(self, token: str, payload: dict[str, Any]) -> None:
        self._post("complete", payload, token)

    def publication_agent_fallback(self, token: str) -> dict[str, Any]:
        return self._post("agent-fallback", {}, token)

    def workflow_start(self, lease_token: str) -> None:
        self._post("start", {"lease_token": lease_token})

    def workflow_heartbeat(
        self, lease_token: str, events: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return self._post("heartbeat", {"lease_token": lease_token, "events": events or []})

    def workflow_policy_review(self, lease_token: str, proposal: dict[str, Any]) -> dict[str, Any]:
        return self._post("policy-review", {"lease_token": lease_token, "proposal": proposal})

    def workflow_model_turn(
        self,
        lease_token: str,
        *,
        step: str,
        profile: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """One model step turn, called by OutcomeCI Cloud with the profile's
        model and key as the pinned revision declares them."""
        return self._post(
            "model-turn",
            {
                "lease_token": lease_token,
                "step": step,
                "profile": profile,
                "messages": messages,
                "tools": tools,
            },
            # The api allows the provider 60 seconds for one turn.
            timeout=MODEL_TURN_TIMEOUT_SECONDS,
        )

    def workflow_credential(self, lease_token: str, reference: str) -> Any:
        return self._post(
            "credentials/resolve",
            {"lease_token": lease_token, "reference": reference},
        ).get("value")

    def workflow_vault_rotate(
        self,
        lease_token: str,
        vault_lease_id: str,
        path: str,
        expected_version: int,
        secrets: dict[str, Any],
    ) -> int:
        """Save secret fields a provider rotated; returns the credential's new version."""
        result = self._post(
            f"vault-leases/{vault_lease_id}/rotate",
            {
                "lease_token": lease_token,
                "path": path,
                "expected_version": expected_version,
                "secrets": secrets,
            },
        )
        return int(result["version"])

    def workflow_agent_fallback(self, lease_token: str) -> dict[str, Any]:
        return self._post("agent-fallback", {"lease_token": lease_token})

    def workflow_pause(self, lease_token: str, **payload) -> None:
        self._post("pause", {"lease_token": lease_token, **payload})

    def workflow_complete(
        self,
        lease_token: str,
        status: str,
        *,
        run_id: str | None = None,
        artifacts: list[dict[str, str]] | None = None,
        category: str | None = None,
        detail: str | None = None,
        expected_credential_version: int | None = None,
        agent_credential: Any | None = None,
        retryable: bool = False,
        resource_usage: ResourceUsageReport | None = None,
    ) -> None:
        self._post(
            "complete",
            {
                "lease_token": lease_token,
                "status": status,
                "run_id": run_id,
                "artifacts": artifacts or [],
                "category": category,
                "detail": detail,
                "expected_credential_version": expected_credential_version,
                "agent_credential": agent_credential,
                "retryable": retryable,
                "resource_usage": resource_usage,
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
