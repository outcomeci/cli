"""Exercise the actual Slack research workflow with controlled decision/search providers."""

import json
import shutil
from pathlib import Path

import httpx
import pytest
from test_model_steps import Model, _call

from outcomeci.broker import executor
from outcomeci.runtime import container, engine
from outcomeci.workflow.compiler import compile_workflow

FIXTURE = Path(__file__).parent / "examples" / "slack-research"


@pytest.mark.parametrize(
    "source,expected_step,topic,domains",
    [
        ("official_docs", "answer_docs", "general", True),
        ("news", "answer_news", "news", False),
        ("general", "answer_general", "general", False),
    ],
)
def test_source_decision_controls_search_and_one_cited_slack_reply(
    tmp_path, monkeypatch, source, expected_step, topic, domains
):
    root = tmp_path / "workflow"
    shutil.copytree(FIXTURE, root)
    config = root / "outcome.yml"
    compiled = compile_workflow(config)
    searches, posts = [], []
    monkeypatch.setattr(executor, "_safe_destination", lambda *args: None)
    original_client = httpx.Client

    def slack(request):
        assert request.url.host == "slack.com"
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "C_TEST", "ts": "2.0"})

    def client(*args, **kwargs):
        return original_client(*args, **{**kwargs, "transport": httpx.MockTransport(slack)})

    monkeypatch.setattr(executor.httpx, "Client", client)

    def search(**kwargs):
        searches.append(kwargs)
        assert kwargs["step"] == expected_step
        assert kwargs["capability"] == "search.search"
        assert kwargs["input"]["topic"] == topic
        assert bool(kwargs["input"]["include_domains"]) is domains
        return {
            "ok": True,
            "status": 200,
            "output": {
                "results": [
                    {"title": "Source", "url": "https://example.com/source", "content": "Evidence"}
                ],
                "usage": {"credits": 1},
                "request_id": "tavily-request",
            },
        }

    def decide(**kwargs):
        assert kwargs["input"]["trigger"]["text"] == "Please answer my question"
        return {
            "answers": [
                {
                    "name": "source",
                    "type": "choice",
                    "choice": source,
                    "confidence": 1.0,
                    "probabilities": [
                        {"value": value, "probability": float(value == source)}
                        for value in ("official_docs", "news", "general")
                    ],
                }
            ],
        }

    def resolve(reference):
        assert reference == "vault:slack/bot-token", "managed search keys left the API"
        return "fake-slack-token"

    model = Model(
        {
            expected_step: [
                {"calls": [_call("search__search", {"query": "a focused question"})]},
                {
                    "calls": [
                        _call("slack__post", {"text": "Answer <https://example.com/source|Source>"})
                    ]
                },
                {
                    "calls": [
                        _call(
                            "return_result",
                            {"status": "answered", "sources": ["https://example.com/source"]},
                        )
                    ]
                },
            ]
        }
    )
    payload = {
        "schema_version": "outcomeci.trigger.dispatcher/v1",
        "type": "dispatcher",
        "event_id": "event",
        "parent_invocation_id": "parent",
        "parent_step": "research",
        "root_invocation_id": "parent",
        "depth": 1,
        "dispatcher": "slack-dispatcher",
        "payload": {
            "channel": "C_TEST",
            "ts": "1.0",
            "user": "U_TEST",
            "text": "Please answer my question",
        },
    }
    result = container.execute(
        root,
        config,
        compiled,
        "dispatcher",
        payload,
        engine.ExecutionOptions(
            decision_client=decide,
            connector_client=search,
            model_client=model,
            credential_resolver=resolve,
            policy_reviewer=lambda proposal: {
                "decision": "allow",
                "proposal_sha256": proposal["proposal_sha256"],
                "reason": "test",
            },
        ),
        auto_continue=True,
    )
    assert result["status"] == "completed"
    assert len(searches) == 1
    assert len(posts) == 1
    assert posts[0]["channel"] == "C_TEST"
    assert posts[0]["thread_ts"] == "1.0"
    assert "https://example.com/source" in posts[0]["text"]
    assert {call["step"] for call in model.calls} == {expected_step}
    artifacts = root / ".outcomeci" / "outcomes" / result["run_id"]
    assert (artifacts / "choose_source" / "decision.json").exists()
    assert (
        json.loads((artifacts / expected_step / "outputs.json").read_text())["status"] == "answered"
    )
