"""Same-origin media fetches stay behind metadata grants and bounded downloads."""

from pathlib import Path

import httpx
import pytest
from test_v1_attachments import IMAGE, _alternatives, _executor, _info

from outcomeci.broker.executor import IntegrationError
from outcomeci.workflow.compiler import ConfigError, _download


def contract(**changes):
    return {
        "path": "/media/{{ input.file }}",
        "query": {"mimeType": "{{ input.mime_type }}"},
        "hosts": ["slack.com"],
        "name": "body.file.name",
        "content_type": "input.mime_type",
        "max_bytes": 1024,
        **changes,
    }


def prepare(tmp_path, monkeypatch, *, info=None, response=None, **changes):
    executor, seen = _executor(tmp_path, monkeypatch, info or _info(), response)
    operation = executor.compiled["workflow"]["spec"]["integrations"]["slack"]["operations"]["file"]
    operation["response"]["download"] = contract(**changes)
    operation["input"]["properties"]["mime_type"] = {"type": "string"}
    return executor, seen


def test_path_download_encodes_query_and_saves_binary(tmp_path, monkeypatch):
    executor, seen = prepare(tmp_path, monkeypatch)
    result = executor.execute(
        "slack.file",
        {"file": "F1", "mime_type": "application/pdf"},
        step="draft",
        response_grants=_alternatives(),
    )
    file = result["output"]["file"]
    assert Path(file["path"]).read_bytes() == IMAGE
    assert file["content_type"] == "application/pdf"
    assert seen[1].url.path == "/media/F1"
    assert seen[1].url.params["mimeType"] == "application/pdf"
    assert seen[1].url.host == "slack.com"
    assert seen[1].headers["authorization"] == "Bearer xoxb-test-token"


def test_path_download_never_precedes_metadata_grant(tmp_path, monkeypatch):
    executor, seen = prepare(tmp_path, monkeypatch, info=_info(channels=("C2",)))
    with pytest.raises(IntegrationError, match="channel must be C1"):
        executor.execute(
            "slack.file",
            {"file": "F1", "mime_type": "text/plain"},
            step="draft",
            response_grants=_alternatives(),
        )
    assert len(seen) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://evil.example/file"}),
        httpx.Response(200, content=b"x" * 1025),
    ],
)
def test_path_download_does_not_follow_redirect_or_save_oversize(tmp_path, monkeypatch, response):
    executor, seen = prepare(tmp_path, monkeypatch, response=response)
    with pytest.raises(IntegrationError):
        executor.execute("slack.file", {"file": "F1", "mime_type": "text/plain"}, step="draft")
    assert len(seen) == 2
    assert not list((tmp_path / "attachments").glob("*"))


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "https://evil.example/file"},
        {"path": "//evil.example/file"},
        {"path": "/file?alt=media"},
        {"url": "body.url"},
        {"query": []},
        {"query": {"nested": {"x": 1}}},
        {"path": "/../file"},
        {"path": "/files\\evil"},
        {"max_bytes": True},
    ],
)
def test_compiler_rejects_ambiguous_or_unsafe_download_contract(changes):
    with pytest.raises(ConfigError):
        _download({"download": contract(**changes)}, "test")


def test_compiler_accepts_path_contract():
    assert _download({"download": contract()}, "test") == {"download": contract()}


def test_exports_of_different_formats_have_distinct_artifacts(tmp_path, monkeypatch):
    executor, _ = prepare(tmp_path, monkeypatch)
    paths = []
    for mime in ("application/pdf", "text/plain"):
        result = executor.execute("slack.file", {"file": "F1", "mime_type": mime}, step="draft")
        paths.append(result["output"]["file"]["path"])
    assert paths[0] != paths[1]
    assert all(Path(path).read_bytes() == IMAGE for path in paths)
