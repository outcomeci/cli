from __future__ import annotations

import httpx
import pytest

from outcomeci import twin


def _env(monkeypatch):
    monkeypatch.setenv("OUTCOMECI_API_URL", "https://api.outcomeci.test")
    monkeypatch.setenv("OUTCOMECI_JOB_ID", "job-1")
    monkeypatch.setenv("OUTCOMECI_JOB_TOKEN", "job-token")


def test_search_requires_an_active_job_capability(monkeypatch):
    monkeypatch.delenv("OUTCOMECI_API_URL", raising=False)
    monkeypatch.delenv("OUTCOMECI_JOB_ID", raising=False)
    monkeypatch.delenv("OUTCOMECI_JOB_TOKEN", raising=False)
    with pytest.raises(twin.TwinError, match="active OutcomeCI job capability"):
        twin.search("query", [], 10, 5)


def test_search_rejects_an_invalid_query(monkeypatch):
    _env(monkeypatch)
    with pytest.raises(twin.TwinError, match="invalid Digital Twin search"):
        twin.search("  ", [], 10, 5)


def test_search_sends_the_bearer_token_and_returns_the_parsed_result(monkeypatch):
    _env(monkeypatch)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={"matches": []})

    result = twin.search(
        "find the pricing service",
        ["owner/repo"],
        10,
        5,
        transport=httpx.MockTransport(handler),
    )

    assert result == {"matches": []}
    assert captured["authorization"] == "Bearer job-token"
    assert captured["url"] == (
        "https://api.outcomeci.test/v1/internal/outcome-jobs/job-1/ontology/search"
    )


def test_search_raises_with_the_status_code_on_an_http_error(monkeypatch):
    _env(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    with pytest.raises(twin.TwinError, match="Digital Twin returned HTTP 503"):
        twin.search("query", [], 10, 5, transport=transport)


def test_search_raises_a_generic_error_on_malformed_json(monkeypatch):
    _env(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(twin.TwinError, match="Digital Twin search failed"):
        twin.search("query", [], 10, 5, transport=transport)


def test_search_rejects_a_non_object_result(monkeypatch):
    _env(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(twin.TwinError, match="invalid response"):
        twin.search("query", [], 10, 5, transport=transport)
