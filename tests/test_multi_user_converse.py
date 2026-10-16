"""A product-engineering pod in one discussion thread, through the real runtime.

The requester (a PM, U_PM) sends the feature request and must approve the
plan; engineers (U_ENG1, U_ENG2) join the thread. `by: trigger.user` stays on
the step throughout. The scenarios: feature intake with back and forth,
disagreement inside the pod, and two requests submitted at the same time.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
import test_v1_slack as slack_example

from outcomeci.broker import executor as integrations
from outcomeci.runtime import container as run_container
from outcomeci.runtime import engine as local
from outcomeci.workflow.compiler import compile_workflow

PM, ENG1, ENG2 = "U_PM", "U_ENG1", "U_ENG2"


class Pod(slack_example.Slack):
    """Threads whose scripted replies name their author: (user, text).

    A reply is released once the runtime has had a chance to read the one
    before it: after the bot answered, or on the next poll after a reply the
    bot did not answer (one it ignored).
    """

    def __init__(self, humans: list[tuple[str, str]], roots: dict[str, dict] | None = None):
        super().__init__([])
        self.threads: dict[str, list[dict]] = {}
        self.scripts: dict[str, list[tuple[str, str]]] = {}
        self.served: dict[str, int] = {}
        for ts, request in (roots or {self.root: slack_example.REQUEST}).items():
            self.threads[ts] = [{"ts": ts, "text": request["text"], "user": request["user"]}]
            self.scripts[ts] = list(humans)
            self.served[ts] = 0
        self.lock = threading.Lock()

    def script(self, ts: str, humans: list[tuple[str, str]]) -> None:
        self.scripts[ts] = list(humans)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            path = request.url.path
            if path == "/api/chat.postMessage":
                body = json.loads(request.content)
                ts = self._ts()
                self.posts.append({**body, "ts": ts})
                response = {"ok": True, "channel": body["channel"], "ts": ts}
                root = body.get("thread_ts")
                if root in self.threads:
                    self.threads[root].append({"ts": ts, "text": body["text"], "bot_id": "B1"})
                    response["message"] = {"thread_ts": root}
                return httpx.Response(200, json=response)
            if path == "/api/conversations.replies":
                root = request.url.params["ts"]
                thread, script = self.threads[root], self.scripts[root]
                last_is_bot = bool(thread[-1].get("bot_id"))
                if script and (last_is_bot or self.served[root] >= len(thread)):
                    user, text = script.pop(0)
                    thread.append({"ts": self._ts(), "user": user, "text": text})
                self.served[root] = len(thread)
                return httpx.Response(200, json={"ok": True, "messages": list(thread)})
            return super().__call__(request)


class PodAgent(slack_example.Agent):
    """The scripted agent, replying in whichever thread the request came from."""

    def __call__(self, runner, model, prompt, root, timeout, **kwargs):
        if "You are taking one turn in a discussion" in prompt:
            return self._turn(prompt)
        context = json.loads(prompt.rsplit("\n", 1)[-1])
        inputs = {item["name"]: item["value"] for item in context["inputs"]}
        if context["step"] == "draft":
            self.steps.append({"runner": runner, "model": model, **context})
            slack_example._call(
                kwargs["extra_env"],
                "slack.post",
                {"text": "Plan: add --json", "thread_ts": _request_ts(inputs["trigger"])},
            )
            Path(context["returns"]["path"]).write_text(
                json.dumps({"plan": slack_example.PLAN}), encoding="utf-8"
            )
            return "draft done"
        if context["step"] == "announce":
            self.steps.append({"runner": runner, "model": model, **context})
            return "announce done"
        return super().__call__(runner, model, prompt, root, timeout, **kwargs)


def _request_ts(trigger: dict) -> str:
    """The Slack message ts of the request, wherever the trigger carries it."""
    import base64

    if isinstance(trigger.get("ts"), str):
        return trigger["ts"]
    data = trigger.get("data")
    if isinstance(data, dict) and isinstance(data.get("ts"), str):
        return data["ts"]
    return json.loads(base64.b64decode(trigger["body_base64"]))["ts"]


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch) -> Path:
    """The example workflow in a fresh directory, as test_v1_slack sets it up."""
    import shutil

    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(slack_example.v1_runtime.time, "sleep", lambda seconds: None)
    return tmp_path / "wf"


@pytest.fixture
def requester(monkeypatch):
    monkeypatch.setitem(slack_example.REQUEST, "user", PM)


def _status(root: Path, run_id: str) -> str:
    return slack_example._consultation(root, run_id)["status"]


def _human_turns(root: Path, run_id: str) -> list[str]:
    turns = slack_example._consultation(root, run_id)["turns"]
    return [turn["message"] for turn in turns if turn["from"] == "human"]


# Feature intake: the pod talks it through, the requester approves.


def test_feature_intake_with_back_and_forth_converges_on_the_requesters_word(
    workflow, requester, monkeypatch
):
    pod = Pod(
        [
            (ENG1, "why --json?"),
            (PM, "also document it"),
            (ENG1, "go ahead"),  # an engineer cannot approve
            (PM, "go ahead"),
        ]
    )
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert result["status"] == "completed"
    assert _status(workflow, result["run_id"]) == "converged"
    assert _human_turns(workflow, result["run_id"]) == [
        "why --json?",
        "also document it",
        "go ahead",
        "go ahead",
    ]
    assert agent.turns == ["why --json?", "also document it", "go ahead", "go ahead"]
    plan = slack_example._consultation(workflow, result["run_id"])["plan"]
    assert plan["summary"] == "Add --json output and document it"
    assert len(pod.pulls) == 2


def test_disagreement_inside_the_pod_is_settled_by_the_requester(workflow, requester, monkeypatch):
    pod = Pod(
        [
            (ENG1, "also document it"),
            (ENG2, "why --json? drop the docs"),
            (PM, "go ahead"),
        ]
    )
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    assert agent.turns == ["also document it", "why --json? drop the docs", "go ahead"]
    plan = slack_example._consultation(workflow, result["run_id"])["plan"]
    assert plan["summary"] == "Add --json output and document it"
    assert len(pod.pulls) == 2


def test_engineers_alone_cannot_approve_so_the_discussion_times_out(
    workflow, requester, monkeypatch
):
    clock = iter(range(0, 10**7, 100_000))
    monkeypatch.setattr(slack_example.v1_runtime.time, "monotonic", lambda: next(clock))
    pod = Pod([(ENG1, "go ahead"), (ENG2, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "timed_out"
    assert result["skipped_steps"] == ["implement", "announce"]
    assert pod.pulls == []


def test_two_replies_in_one_poll_get_one_answer_that_saw_both(workflow, requester, monkeypatch):
    class Burst(Pod):
        """The first poll finds two replies at once: ENG2's and the next scripted one."""

        burst = False

        def __call__(self, request):
            if request.url.path == "/api/conversations.replies" and not self.burst:
                root = request.url.params["ts"]
                thread = self.threads[root]
                if thread[-1].get("bot_id"):
                    self.burst = True
                    thread.append({"ts": self._ts(), "user": ENG2, "text": "why --json?"})
                    user, text = self.scripts[root].pop(0)
                    thread.append({"ts": self._ts(), "user": user, "text": text})
            return super().__call__(request)

    pod = Burst([(ENG1, "also document it"), (PM, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    assert _human_turns(workflow, result["run_id"]) == [
        "why --json?",
        "also document it",
        "go ahead",
    ]
    assert agent.turns == ["also document it", "go ahead"]


# Two requests submitted at the same time, by two requesters, in one channel.


def _payload_for(request: dict) -> dict:
    import base64

    payload = slack_example._payload()
    payload["event_id"] = f"evt-{request['ts']}"
    payload["body_base64"] = base64.b64encode(json.dumps(request).encode()).decode()
    return payload


def test_simultaneous_submissions_run_in_their_own_threads(workflow, requester, monkeypatch):
    first = {**slack_example.REQUEST, "user": PM, "ts": "100.0"}
    second = {
        **slack_example.REQUEST,
        "user": ENG1,
        "ts": "101.0",
        "text": "Add `outcomeci/cli` retries and `outcomeci/api` limits",
    }
    pod = Pod([], roots={"100.0": first, "101.0": second})
    pod.script("100.0", [(ENG1, "why --json?"), (PM, "go ahead")])
    pod.script("101.0", [(PM, "go ahead"), (ENG1, "go ahead")])  # here the PM is the colleague
    agent = PodAgent()

    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(pod)
        return real(*args, **kwargs)

    monkeypatch.setattr(integrations.httpx, "Client", client)
    monkeypatch.setattr(local, "invoke", agent)
    config = workflow / slack_example.WORKFLOW
    compiled = compile_workflow(config)
    options = local.ExecutionOptions(
        credential_resolver=lambda reference: "xoxb-test-credential",
        policy_reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
    )
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def run(request):
        try:
            results[request["ts"]] = run_container.execute(
                workflow,
                config,
                compiled,
                "webhook",
                _payload_for(request),
                options,
                auto_continue=True,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=run, args=(request,)) for request in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    assert not errors, errors
    assert set(results) == {"100.0", "101.0"}
    assert results["100.0"]["run_id"] != results["101.0"]["run_id"]
    for ts, result in results.items():
        assert result["status"] == "completed", (ts, result)
        assert _status(workflow, result["run_id"]) == "converged"
    assert _human_turns(workflow, results["100.0"]["run_id"]) == ["why --json?", "go ahead"]
    assert _human_turns(workflow, results["101.0"]["run_id"]) == ["go ahead", "go ahead"]
    assert len(pod.pulls) == 4
    # each thread's replies stayed in that thread
    for root, thread in pod.threads.items():
        assert all(
            message["ts"] == root or message.get("bot_id") or message["user"] for message in thread
        )
