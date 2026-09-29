"""Files attached to Slack messages: fetched only where shared, saved for the agent."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import test_v1_sentry as sentry
import test_v1_slack as slack_example

from outcomeci import v1_runtime
from outcomeci.config import compile_workflow
from outcomeci.integrations import IntegrationError, IntegrationExecutor
from outcomeci.policy import PolicyExecutor

SHARED_IN = ["body.file.channels", "body.file.groups", "body.file.ims"]
IMAGE = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _info(*, channels=("C1",), url="https://files.slack.com/files-pri/T1-F1/shot.png"):
    return {
        "ok": True,
        "file": {
            "id": "F1",
            "name": "shot.png",
            "mimetype": "image/png",
            "size": len(IMAGE),
            "channels": list(channels),
            "groups": [],
            "ims": [],
            "url_private_download": url,
        },
    }


def _executor(tmp_path: Path, monkeypatch, info: dict, file_response=None):
    sent = []

    def transport(request):
        sent.append(request)
        if request.url.path == "/api/files.info":
            return httpx.Response(200, json=info)
        return file_response or httpx.Response(200, content=IMAGE)

    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)
    compiled = compile_workflow(slack_example.EXAMPLES / slack_example.WORKFLOW)
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda ref: "xoxb-test-token",
        transport=httpx.MockTransport(transport),
        reviewed=True,
        downloads=tmp_path / "attachments",
    )
    return executor, sent


def _grant(channel="C1"):
    return [{"name": "channel", "paths": SHARED_IN, "granted": channel}]


def _alternatives(channel="C1"):
    return [_grant(channel)]


def test_a_file_shared_in_the_granted_channel_is_saved_for_the_agent(tmp_path, monkeypatch):
    executor, sent = _executor(tmp_path, monkeypatch, _info())

    result = executor.execute(
        "slack.file", {"file": "F1"}, step="draft", response_grants=_alternatives()
    )

    file = result["output"]["file"]
    assert Path(file["path"]).read_bytes() == IMAGE
    assert Path(file["path"]).parent == tmp_path / "attachments"
    assert file["name"] == "shot.png" and file["content_type"] == "image/png"
    assert result["output"]["mimetype"] == "image/png"
    download = sent[1]
    assert download.url.host == "files.slack.com"
    assert download.headers["authorization"] == "Bearer xoxb-test-token"
    assert "files.slack.com" not in str(result["output"])


def test_a_file_not_shared_in_the_granted_channel_is_never_downloaded(tmp_path, monkeypatch):
    executor, sent = _executor(tmp_path, monkeypatch, _info(channels=("C2",)))

    with pytest.raises(IntegrationError, match="channel must be C1"):
        executor.execute(
            "slack.file", {"file": "F1"}, step="draft", response_grants=_alternatives()
        )

    assert [request.url.path for request in sent] == ["/api/files.info"]
    assert not (tmp_path / "attachments").exists()


def test_a_download_from_another_host_is_refused(tmp_path, monkeypatch):
    info = _info(url="https://evil.example.com/steal")
    executor, sent = _executor(tmp_path, monkeypatch, info)

    with pytest.raises(IntegrationError, match="only from files.slack.com"):
        executor.execute(
            "slack.file", {"file": "F1"}, step="draft", response_grants=_alternatives()
        )

    assert len(sent) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)), "larger than"),
        (httpx.Response(302, headers={"location": "https://slack.com/signin"}), "HTTP 302"),
    ],
    ids=["too large", "redirected to sign-in"],
)
def test_a_download_that_cannot_complete_fails(tmp_path, monkeypatch, response, message):
    executor, _ = _executor(tmp_path, monkeypatch, _info(), file_response=response)

    with pytest.raises(IntegrationError, match=message):
        executor.execute(
            "slack.file", {"file": "F1"}, step="draft", response_grants=_alternatives()
        )

    assert not list((tmp_path / "attachments").glob("*"))


def test_a_response_grant_becomes_a_check_for_the_executor(tmp_path):
    compiled = compile_workflow(slack_example.EXAMPLES / slack_example.WORKFLOW)
    policy = PolicyExecutor(
        IntegrationExecutor(compiled),
        tmp_path,
        {},
        grants=[{"capability": "slack.file", "args": {"channel": "C1"}, "as": None}],
    )

    request, _, alternatives = policy._apply_grants("slack.file", {"file": "F1"})

    assert request == {"file": "F1"}
    assert alternatives == [(None, _grant())]


def test_the_slack_example_lets_its_draft_step_open_the_requests_files():
    compiled = compile_workflow(slack_example.EXAMPLES / slack_example.WORKFLOW)
    steps = compiled["instructions"]["steps"]

    assert "slack.file" in steps["draft"]["capabilities"]
    assert "slack.file" in steps["discuss"]["capabilities"]
    assert steps["discuss"]["v1"]["converse"]["attachment"] == "file"


def test_a_reply_file_is_fetched_only_within_its_threads_conversation(tmp_path, monkeypatch):
    executor, _ = _executor(tmp_path, monkeypatch, _info(channels=("C9",)))
    spec = {"api": "slack", "attachment": "file"}
    item = {"id": "F1", "name": "shot.png"}

    refused = v1_runtime._attachment(executor, spec, "C1", item, "discuss")
    fetched = v1_runtime._attachment(executor, spec, "C9", item, "discuss")

    assert refused["name"] == "shot.png" and "channel must be C1" in refused["error"]
    assert Path(fetched["path"]).read_bytes() == IMAGE
    assert v1_runtime._attachment(executor, {"api": "slack"}, "C9", item, "discuss")["error"]


def _broker(tmp_path, monkeypatch, info, grants, *, step_policy=None):
    executor, sent = _executor(tmp_path, monkeypatch, info)
    policy = PolicyExecutor(
        executor,
        tmp_path / "broker",
        {},
        reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
        grants=grants,
        step_policy=step_policy,
    )
    return policy, sent


@pytest.mark.parametrize(
    "step_policy",
    [None, {"content": "Read only what the request attached.", "policy": {}}],
    ids=["direct", "reviewed"],
)
def test_the_broker_refuses_a_file_outside_the_granted_channel(tmp_path, monkeypatch, step_policy):
    grants = [{"capability": "slack.file", "args": {"channel": "C1"}, "as": None}]
    policy, sent = _broker(
        tmp_path, monkeypatch, _info(channels=("C2",)), grants, step_policy=step_policy
    )

    with pytest.raises(IntegrationError, match="channel must be C1"):
        policy.execute("slack.file", {"file": "F1"}, step="draft")

    assert [request.url.path for request in sent] == ["/api/files.info"]


def test_any_grant_that_holds_opens_the_file_and_is_recorded(tmp_path, monkeypatch):
    grants = [
        {"capability": "slack.file", "args": {"channel": "C1"}, "as": "here"},
        {"capability": "slack.file", "args": {"channel": "C2"}, "as": "there"},
    ]
    policy, _ = _broker(
        tmp_path,
        monkeypatch,
        _info(channels=("C2",)),
        grants,
        step_policy={"content": "Read attachments.", "policy": {}},
    )

    result = policy.execute("slack.file", {"file": "F1"}, step="draft")

    assert Path(result["output"]["file"]["path"]).read_bytes() == IMAGE
    journal = json.loads((tmp_path / "broker" / "journal.json").read_text())
    (call,) = journal["calls"].values()
    assert call["as"] == "there"


def test_a_download_is_saved_with_the_runs_artifacts(tmp_path):
    from outcomeci.cloud_runner.main import workflow_artifacts
    from outcomeci.integrations import attachments_path

    target = attachments_path(tmp_path, "run-1") / "abc-shot.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(IMAGE)

    (artifact,) = workflow_artifacts(tmp_path, "run-1")

    assert artifact["path"] == ".outcomeci/outcomes/run-1/attachments/abc-shot.png"
