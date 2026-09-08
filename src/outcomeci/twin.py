"""Revision-pinned OutcomeCI Digital Twin client."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class TwinError(RuntimeError):
    pass


def search(query: str, repository_ids: list[str], limit: int, component_limit: int) -> dict[str, Any]:
    api_url = os.environ.get("OUTCOMECI_API_URL", "").rstrip("/")
    job_id = os.environ.get("OUTCOMECI_JOB_ID", "")
    token = os.environ.get("OUTCOMECI_JOB_TOKEN", "")
    if not api_url or not job_id or not token:
        raise TwinError("an active OutcomeCI job capability is required")
    if not query.strip() or len(query) > 2000 or not 1 <= limit <= 100 or not 1 <= component_limit <= 20:
        raise TwinError("invalid Digital Twin search")
    body = {"query": query, "repository_ids": repository_ids, "limit": limit, "component_limit": component_limit, "stale_after_hours": 24}
    request = urllib.request.Request(
        f"{api_url}/v1/internal/outcome-jobs/{job_id}/ontology/search",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read(1_048_577))
    except urllib.error.HTTPError as exc:
        raise TwinError(f"Digital Twin returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TwinError("Digital Twin search failed") from exc
    if not isinstance(result, dict):
        raise TwinError("Digital Twin returned an invalid response")
    return result

