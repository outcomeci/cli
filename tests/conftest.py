"""Shared test setup: no test clones a repository from the network."""

from __future__ import annotations

import pytest

from outcomeci import checkouts


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "repository_checkouts: run the real local repository checkouts"
    )


@pytest.fixture(autouse=True)
def _no_repository_checkouts(request, monkeypatch):
    """A step's agent gets no checkouts unless its test opts in, and one that
    does clones from a local repository, never from github.com."""
    if request.node.get_closest_marker("repository_checkouts") is None:
        monkeypatch.setattr(checkouts, "prepare", lambda *args, **kwargs: [])
    else:
        monkeypatch.setattr(checkouts, "clone_url", _refused)


def _refused(owner: str, name: str) -> str:
    raise checkouts.CheckoutError("tests clone only from local repositories")
