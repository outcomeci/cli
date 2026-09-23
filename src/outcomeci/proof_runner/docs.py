"""Fetch and parse published doc pages for the docs-fidelity proof.

Doc pages mark the snippets meant to be proof-tested with an HTML comment
immediately above the fenced code block, invisible in the rendered page:

    <!-- proof:cmd id="init" -->
    ```sh
    oci init --backend filesystem
    ```

This module fetches the raw Markdown over HTTP (never a copy checked into
this repo) and extracts those marked blocks, so the proof exercises whatever
is actually published under /docs/outcomeci/next.
"""

from __future__ import annotations

import os
import re

import httpx

from ..process import ExecutionError

DEFAULT_DOCS_BASE_URL = "https://sparepartslabs.com/api/docs/raw/outcomeci/next"

_MARKER = re.compile(
    r'<!--\s*proof:(?P<kind>cmd|yaml|path)\s+id="(?P<id>[a-z0-9][a-z0-9_-]*)"\s*-->'
    r"\s*\n```[a-zA-Z0-9]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def docs_base_url() -> str:
    # `or` rather than `.get(..., default)`: an env var set to "" (as GitHub Actions
    # does for an unset repo variable interpolated into `env:`) must still fall back.
    return (os.environ.get("OUTCOMECI_DOCS_BASE_URL") or DEFAULT_DOCS_BASE_URL).rstrip("/")


def fetch_page(page: str) -> str:
    url = f"{docs_base_url()}/{page}"
    try:
        response = httpx.get(url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExecutionError(f"could not fetch doc page {page!r} from {url}: {exc}") from exc
    return response.text


def extract_fixtures(content: str) -> dict[str, dict[str, str]]:
    fixtures: dict[str, dict[str, str]] = {}
    for match in _MARKER.finditer(content):
        fixture_id = match.group("id")
        if fixture_id in fixtures:
            raise ExecutionError(f"duplicate doc proof fixture id {fixture_id!r}")
        fixtures[fixture_id] = {"kind": match.group("kind"), "body": match.group("body")}
    return fixtures


def fetch_fixtures(page: str) -> dict[str, dict[str, str]]:
    fixtures = extract_fixtures(fetch_page(page))
    if not fixtures:
        raise ExecutionError(f"doc page {page!r} has no marked proof fixtures")
    return fixtures
