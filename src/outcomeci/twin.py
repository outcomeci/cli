"""Revision-pinned OutcomeCI Digital Twin client."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx


class TwinError(RuntimeError):
    pass


def search(
    query: str,
    repository_ids: list[str],
    limit: int,
    component_limit: int,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    api_url = os.environ.get("OUTCOMECI_API_URL", "").rstrip("/")
    job_id = os.environ.get("OUTCOMECI_JOB_ID", "")
    token = os.environ.get("OUTCOMECI_JOB_TOKEN", "")
    if not api_url or not job_id or not token:
        raise TwinError("an active OutcomeCI job capability is required")
    if (
        not query.strip()
        or len(query) > 2000
        or not 1 <= limit <= 100
        or not 1 <= component_limit <= 20
    ):
        raise TwinError("invalid Digital Twin search")
    body = {
        "query": query,
        "repository_ids": repository_ids,
        "limit": limit,
        "component_limit": component_limit,
        "stale_after_hours": 24,
    }
    url = f"{api_url}/v1/internal/outcome-jobs/{job_id}/ontology/search"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with (
            httpx.Client(transport=transport, timeout=30, follow_redirects=True) as client,
            client.stream("POST", url, json=body, headers=headers) as response,
        ):
            if response.status_code >= 400:
                raise TwinError(f"Digital Twin returned HTTP {response.status_code}")
            # Bounded read, not response.json(): an unbounded body from a
            # broken or malicious endpoint must not be pulled fully into
            # memory before we even look at it.
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 1_048_576:
                    break
            result = json.loads(bytes(raw))
    except httpx.HTTPError as exc:
        raise TwinError("Digital Twin search failed") from exc
    except json.JSONDecodeError as exc:
        raise TwinError("Digital Twin search failed") from exc
    if not isinstance(result, dict):
        raise TwinError("Digital Twin returned an invalid response")
    return result
