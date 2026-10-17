"""A product-engineering pod in one discussion thread, through the real runtime.

The requester (a PM, U_PM) sends the feature request; engineers (U_ENG1,
U_ENG2) join the thread. The example workflow lets the requester alone decide
(`decided_by: requester`); the later scenarios rewrite its `humans` and the
discuss step to try groups, channels, an email, majority and unanimous rules,
an override, and what the deadline does.
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
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow.compiler import compile_workflow

PM, ENG1, ENG2 = "U_PM", "U_ENG1", "U_ENG2"
OWNER_EMAIL = "owner@example.com"
DIRECTORY = {
    "emails": {OWNER_EMAIL: PM},
    "usergroups": [{"id": "S0POD", "handle": "product-eng"}],
    "group_members": {"S0POD": [ENG1, ENG2]},
    "channels": [{"id": "C0BUILD", "name": "growth"}],
    "members": {"C0BUILD": [PM, ENG1, ENG2, "B_BOT"]},
}


class Pod(slack_example.Slack):
    """Threads whose scripted replies name their author: (user, text).

    A reply is released once the runtime has had a chance to read the one
    before it: after the bot answered, or on the next poll after a reply the
    bot did not answer (one it ignored).
    """

    def __init__(
        self,
        humans: list[tuple[str, str]],
        roots: dict[str, dict] | None = None,
        hold_until: str | None = None,
    ):
        super().__init__([])
        self.threads: dict[str, list[dict]] = {}
        self.scripts: dict[str, list[tuple[str, str]]] = {}
        self.served: dict[str, int] = {}
        # Hold every scripted reply until the bot has posted this text.
        self.hold_until = hold_until
        self.lookups: list[str] = []
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
                held = self.hold_until is not None and not any(
                    message.get("bot_id") and self.hold_until in message["text"]
                    for message in thread
                )
                if script and not held and (last_is_bot or self.served[root] >= len(thread)):
                    user, text = script.pop(0)
                    thread.append({"ts": self._ts(), "user": user, "text": text})
                self.served[root] = len(thread)
                return httpx.Response(200, json={"ok": True, "messages": list(thread)})
            if path.startswith("/api/") and path not in {"/api/files.info"}:
                self.lookups.append(path)
                params = request.url.params
                if path == "/api/users.lookupByEmail":
                    user = DIRECTORY["emails"].get(params["email"])
                    if user is None:
                        return httpx.Response(200, json={"ok": False, "error": "users_not_found"})
                    return httpx.Response(200, json={"ok": True, "user": {"id": user, "name": "o"}})
                if path == "/api/usergroups.list":
                    return httpx.Response(
                        200, json={"ok": True, "usergroups": DIRECTORY["usergroups"]}
                    )
                if path == "/api/usergroups.users.list":
                    return httpx.Response(
                        200,
                        json={"ok": True, "users": DIRECTORY["group_members"][params["usergroup"]]},
                    )
                if path == "/api/conversations.list":
                    return httpx.Response(200, json={"ok": True, "channels": DIRECTORY["channels"]})
                if path == "/api/conversations.members":
                    return httpx.Response(
                        200, json={"ok": True, "members": DIRECTORY["members"][params["channel"]]}
                    )
            return super().__call__(request)


class PodAgent(slack_example.Agent):
    """The scripted agent, replying in whichever thread the request came from.

    A turn answers the newest messages: "go ahead" approves, "reject it"
    rejects, "also ..." revises, anything else is answered. When more than one
    person spoke since the last answer, each decider's word goes in `votes`.
    """

    def _turn(self, prompt: str) -> str:
        import re

        path = Path(re.search(r"to (/\S+\.json)\.", prompt).group(1))
        plan = json.loads(re.search(r"Current plan \(version \d+\): (.+)\n", prompt).group(1))
        turns = json.loads(re.search(r"Discussion so far: (.+)\n", prompt).group(1))
        batch = []
        for turn in reversed(turns):
            if turn["from"] != "human":
                break
            batch.insert(0, turn)
        latest = batch[-1]["message"]
        self.turns.append(latest)
        words = {"go ahead": "approve", "reject it": "reject"}
        votes = {turn["user"]: words[turn["message"]] for turn in batch if turn["message"] in words}
        if latest.startswith("also"):
            plan = {**plan, "summary": plan["summary"] + " and document it"}
            answer = {"status": "revised", "plan": plan, "message": "Updated: also document it."}
        elif latest == "go ahead":
            answer = {"status": "converged", "plan": plan, "message": "Starting now."}
        elif latest == "reject it":
            answer = {"status": "rejected", "plan": plan, "message": "Understood, dropping it."}
        else:
            answer = {"status": "answered", "plan": plan, "message": "Because scripts parse it."}
        if len(votes) > 1:
            answer["votes"] = votes
        path.write_text(json.dumps(answer), encoding="utf-8")
        return "turn done"

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


def _variant(workflow: Path, humans: str, discuss: str) -> None:
    """Rewrite the example's humans block and the discuss step's decision."""
    path = workflow / slack_example.WORKFLOW
    text = path.read_text()
    head, _, rest = text.partition("humans:\n")
    _, _, tail = rest.partition("\n\n")
    text = head + "humans:\n" + humans.rstrip("\n") + "\n\n" + tail
    marker = "      decided_by: requester  # others may discuss; only the requester approves\n"
    assert marker in text
    path.write_text(text.replace(marker, discuss))


def _verdict(root: Path, run_id: str) -> dict:
    return slack_example._consultation(root, run_id)["verdict"]


def _texts(pod: Pod) -> list[str]:
    return [post["text"] for post in pod.posts]


POD_AND_OWNER = f"""\
  pod:   {{system: slack, type: group, identifier: "@product-eng"}}
  owner: {{system: slack, type: email, identifier: {OWNER_EMAIL}}}
"""
ROOM = """\
  room: {system: slack, type: channel, identifier: "#growth"}
"""


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


# Groups, rules, an override and the deadline.


def test_a_user_group_decides_unanimously_and_an_email_names_the_override(
    workflow, requester, monkeypatch
):
    _variant(
        workflow,
        POD_AND_OWNER,
        "      decided_by: pod\n      rule: all\n      override: owner\n      returns: [plan, status, decision, verdict]\n",
    )
    pod = Pod([(ENG1, "go ahead"), (PM, "go ahead")])  # the PM is the owner here, not in the pod
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert result["status"] == "completed"
    assert sorted(set(pod.lookups)) == [
        "/api/usergroups.list",
        "/api/usergroups.users.list",
        "/api/users.lookupByEmail",
    ]
    assert _status(workflow, result["run_id"]) == "converged"
    verdict = _verdict(workflow, result["run_id"])
    assert verdict["how"] == "override"
    assert verdict["override_by"] == PM
    assert verdict["votes"] == {ENG1: "approve", PM: "approve"}
    texts = _texts(pod)
    assert (
        f"This plan is decided by <@{ENG1}>, <@{ENG2}> (2 of 2 must approve); <@{PM}>" in texts[2]
    )
    assert "1 of 2 approved; 2 needed (all)." in texts
    assert any(text.startswith(f"Closed by <@{PM}>: approved.") for text in texts)
    assert len(pod.pulls) == 2


def test_a_channel_majority_counts_the_latest_word_of_each_person(workflow, requester, monkeypatch):
    _variant(workflow, ROOM, "      decided_by: room\n      rule: majority\n")
    pod = Pod([(ENG1, "go ahead"), (ENG2, "reject it"), (ENG2, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert sorted(set(pod.lookups)) == ["/api/conversations.list", "/api/conversations.members"]
    assert _status(workflow, result["run_id"]) == "converged"
    verdict = _verdict(workflow, result["run_id"])
    assert verdict == {
        "outcome": "approved",
        "how": "rule",
        "rule": "majority",
        "of": [ENG1, ENG2, PM],
        "votes": {ENG1: "approve", ENG2: "approve"},
    }
    assert "1 of 3 approved; 2 needed (majority)." in _texts(pod)
    assert "1 of 3 approved, 1 rejected; 2 needed (majority)." in _texts(pod)


def test_mixed_votes_in_one_poll_are_counted_per_person(workflow, requester, monkeypatch):
    class Burst(Pod):
        burst = False

        def __call__(self, request):
            if request.url.path == "/api/conversations.replies" and not self.burst:
                root = request.url.params["ts"]
                thread = self.threads[root]
                if thread[-1].get("bot_id"):
                    self.burst = True
                    thread.append({"ts": self._ts(), "user": ENG1, "text": "reject it"})
                    thread.append({"ts": self._ts(), "user": ENG2, "text": "go ahead"})
            return super().__call__(request)

    _variant(workflow, ROOM, "      decided_by: room\n      rule: majority\n")
    pod = Burst([(PM, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert agent.turns == ["go ahead", "go ahead"]
    assert _status(workflow, result["run_id"]) == "converged"
    assert _verdict(workflow, result["run_id"])["votes"] == {
        ENG1: "reject",
        ENG2: "approve",
        PM: "approve",
    }


def test_a_rejection_by_enough_people_closes_the_plan(workflow, requester, monkeypatch):
    _variant(workflow, ROOM, "      decided_by: room\n      rule: majority\n")
    pod = Pod([(ENG1, "reject it"), (PM, "reject it")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "rejected"
    assert _verdict(workflow, result["run_id"])["outcome"] == "rejected"
    assert result["skipped_steps"] == ["implement", "announce"]
    assert pod.pulls == []


def test_an_override_can_reject_as_well_as_approve(workflow, requester, monkeypatch):
    _variant(
        workflow, POD_AND_OWNER, "      decided_by: pod\n      rule: all\n      override: owner\n"
    )
    pod = Pod([(ENG1, "go ahead"), (PM, "reject it")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "rejected"
    verdict = _verdict(workflow, result["run_id"])
    assert (verdict["outcome"], verdict["how"], verdict["override_by"]) == (
        "rejected",
        "override",
        PM,
    )
    assert any(text.startswith(f"Closed by <@{PM}>: rejected.") for text in _texts(pod))
    assert result["skipped_steps"] == ["implement", "announce"]
    assert pod.pulls == []


def test_one_rejection_sinks_a_unanimous_plan(workflow, requester, monkeypatch):
    _variant(workflow, POD_AND_OWNER, "      decided_by: pod\n      rule: all\n")
    pod = Pod([(ENG2, "go ahead"), (ENG1, "reject it")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "rejected"
    assert _verdict(workflow, result["run_id"])["votes"] == {ENG2: "approve", ENG1: "reject"}
    assert pod.pulls == []


def test_a_count_rule_needs_that_many_approvals(workflow, requester, monkeypatch):
    _variant(workflow, ROOM, "      decided_by: room\n      rule: 2\n")
    pod = Pod([(ENG1, "go ahead"), (ENG2, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    assert "1 of 3 approved; 2 needed (2)." in _texts(pod)
    assert _verdict(workflow, result["run_id"])["rule"] == "2"
    assert len(pod.pulls) == 2


def test_a_vote_the_agent_attributes_to_an_outsider_is_not_counted(
    workflow, requester, monkeypatch
):
    class Burst(Pod):
        burst = False

        def __call__(self, request):
            if request.url.path == "/api/conversations.replies" and not self.burst:
                root = request.url.params["ts"]
                thread = self.threads[root]
                if thread[-1].get("bot_id"):
                    self.burst = True
                    thread.append({"ts": self._ts(), "user": "U_OUT", "text": "go ahead"})
                    thread.append({"ts": self._ts(), "user": ENG1, "text": "go ahead"})
            return super().__call__(request)

    _variant(workflow, ROOM, "      decided_by: room\n      rule: majority\n")
    pod = Burst([(PM, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert "1 of 3 approved; 2 needed (majority)." in _texts(pod)
    assert _verdict(workflow, result["run_id"])["votes"] == {ENG1: "approve", PM: "approve"}


def test_a_run_whose_humans_cannot_be_found_fails_before_any_step(workflow, requester, monkeypatch):
    _variant(
        workflow,
        "  owner: {system: slack, type: email, identifier: nobody@example.com}\n"
        "  requester: {system: slack, type: user, identifier: trigger.user}\n",
        "      decided_by: owner\n",
    )
    pod = Pod([])
    agent = PodAgent()

    with pytest.raises(Exception, match="no Slack user has the address nobody@example.com"):
        slack_example._run(workflow, pod, agent, monkeypatch)

    assert agent.steps == []
    assert pod.posts == []
    runs = list((workflow / ".outcomeci/outcomes").iterdir())
    state = json.loads((runs[0] / "run.json").read_text())
    assert state["status"] == "error"
    assert state["error"] == "humans.owner: no Slack user has the address nobody@example.com"


def test_steps_can_address_humans_in_grants_and_inputs(workflow, requester, monkeypatch):
    _variant(workflow, POD_AND_OWNER + ROOM, "      decided_by: requester\n")
    path = workflow / slack_example.WORKFLOW
    text = path.read_text()
    text = text.replace(
        "        - slack.post: {channel: trigger.channel, thread_ts: trigger.ts}",
        "        - slack.post: {channel: humans.room.id, thread_ts: trigger.ts}",
    ).replace(
        "      with: [implement, discuss.plan]",
        "      with: [implement, discuss.plan, humans.pod.users, humans.owner]",
    )
    path.write_text(
        text.replace(
            "humans:\n",
            "humans:\n  requester: {system: slack, type: user, identifier: trigger.user}\n",
        )
    )
    pod = Pod([(PM, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert result["status"] == "completed"
    announce = next(step for step in agent.steps if step["step"] == "announce")
    assert announce["grants"][0]["args"]["channel"] == "C0BUILD"
    inputs = {item["name"]: item["value"] for item in announce["inputs"]}
    assert inputs["users"] == [ENG1, ENG2]
    assert inputs["owner"]["id"] == PM


def test_a_parked_discussion_asks_the_owner_and_parks_again_until_they_answer(
    tmp_path, monkeypatch
):
    """The managed path: the api wakes the run at the deadline, the runtime
    asks the owner, parks again on a later deadline, and the owner's reply
    arrives through the api's captured output."""
    import shutil

    from outcomeci.cloud_runner import checkpoint

    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(slack_example.v1_runtime.time, "sleep", lambda seconds: None)
    monkeypatch.setitem(slack_example.REQUEST, "user", PM)
    root = tmp_path / "wf"
    _variant(
        root,
        POD_AND_OWNER,
        "      decided_by: pod\n      rule: all\n      otherwise: {ask: owner, timeout: 1h}\n",
    )
    config = root / slack_example.WORKFLOW
    pod, agent = Pod([]), PodAgent()
    real = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(pod)}),
    )
    monkeypatch.setattr(local, "invoke", agent)
    options = local.ExecutionOptions(
        durable_waits=True,
        credential_resolver=lambda ref: "xoxb-test",
        policy_reviewer=lambda p: {
            "decision": "allow",
            "proposal_sha256": p["proposal_sha256"],
            "reason": "ok",
        },
    )
    first = local.trigger(root, config, "webhook", slack_example._payload(), options=options)
    with pytest.raises(slack_example.v1_runtime.DurableWait) as parked:
        local.continue_run(root, config, first["run_id"], approve=True, options=options)
    run_id = first["run_id"]
    first_deadline = parked.value.interaction["expires_at"]
    consultation_path = root / ".outcomeci/outcomes" / run_id / "discuss/consultation.json"
    consultation = json.loads(consultation_path.read_text())
    assert consultation["deciders"]["of"] == [ENG1, ENG2]
    assert f"This plan is decided by <@{ENG1}>, <@{ENG2}> (2 of 2 must approve)." in _texts(pod)

    # The api wakes the run after the deadline with no reply.
    consultation["expires_at"] = 1
    consultation_path.write_text(json.dumps(consultation))
    options.resume_response = {"reason": "timeout"}
    with pytest.raises(slack_example.v1_runtime.DurableWait) as again:
        local.resume_wait(root, config, run_id, options=options)
    assert _texts(pod)[-1] == (
        f"The deadline passed without a decision. <@{PM}>, your approval or rejection decides it."
    )
    # The step's own deadline (a day) was moved to the past; the ask gives
    # the owner an hour from now, so the new deadline is earlier than the
    # first one and later than now.
    from datetime import UTC, datetime

    asked_until = datetime.fromisoformat(again.value.interaction["expires_at"])
    assert datetime.now(UTC) < asked_until < datetime.fromisoformat(first_deadline)
    consultation = json.loads(consultation_path.read_text())
    assert consultation["deciders"]["asked"] is True
    assert consultation["deciders"]["of"] == [PM]
    checkpoint.capture(root, run_id)  # the parked state is a valid checkpoint

    # An engineer's reply no longer decides; the owner's does.
    options.resume_response = {
        "reason": "reply",
        "output": {
            "messages": [{"ts": "100.0"}, {"ts": "900.0", "text": "go ahead", "user": ENG1}]
        },
    }
    with pytest.raises(slack_example.v1_runtime.DurableWait):
        local.resume_wait(root, config, run_id, options=options)
    assert f"Noted. The plan is decided by <@{PM}>." in _texts(pod)
    options.resume_response = {
        "reason": "reply",
        "output": {"messages": [{"ts": "100.0"}, {"ts": "901.0", "text": "go ahead", "user": PM}]},
    }
    state = local.resume_wait(root, config, run_id, options=options)
    while state["status"] == "awaiting_confirmation":
        state = local.continue_run(root, config, run_id, approve=True, options=options)
    assert state["status"] == "completed"
    assert _verdict(root, run_id)["votes"] == {PM: "approve"}
    assert len(pod.pulls) == 2


def test_a_revised_plan_needs_fresh_votes(workflow, requester, monkeypatch):
    _variant(workflow, ROOM, "      decided_by: room\n      rule: majority\n")
    pod = Pod(
        [(ENG1, "go ahead"), (ENG2, "also document it"), (ENG2, "go ahead"), (PM, "go ahead")]
    )
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    verdict = _verdict(workflow, result["run_id"])
    # ENG1 approved version 1 only; version 2 was carried by ENG2 and the PM.
    assert verdict["votes"] == {ENG2: "approve", PM: "approve"}


def _stall_the_clock(monkeypatch):
    clock = iter(range(0, 10**9, 100_000))
    monkeypatch.setattr(slack_example.v1_runtime.time, "monotonic", lambda: next(clock))


def test_the_deadline_can_continue_without_everyone(workflow, requester, monkeypatch):
    _stall_the_clock(monkeypatch)
    _variant(
        workflow,
        POD_AND_OWNER,
        "      decided_by: pod\n      rule: all\n      otherwise: continue\n      returns: [plan, status, decision, verdict]\n",
    )
    pod = Pod([(ENG1, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    verdict = _verdict(workflow, result["run_id"])
    assert (verdict["outcome"], verdict["how"]) == ("approved", "timeout")
    assert "No decision by the deadline. Continuing with plan v1 as it stands." in _texts(pod)
    assert len(pod.pulls) == 2


def test_the_deadline_can_relax_the_rule_to_the_votes_cast(workflow, requester, monkeypatch):
    _stall_the_clock(monkeypatch)
    _variant(
        workflow,
        POD_AND_OWNER,
        "      decided_by: pod\n      rule: all\n      otherwise: {rule: any}\n",
    )
    pod = Pod([(ENG1, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    verdict = _verdict(workflow, result["run_id"])
    assert (verdict["how"], verdict["rule"], verdict["votes"]) == (
        "relaxed",
        "any",
        {ENG1: "approve"},
    )
    assert len(pod.pulls) == 2


def test_the_deadline_can_ask_the_owner_whose_word_then_decides(workflow, requester, monkeypatch):
    _stall_the_clock(monkeypatch)
    _variant(
        workflow,
        POD_AND_OWNER,
        "      decided_by: pod\n      rule: all\n      otherwise: {ask: owner, timeout: 1h}\n",
    )
    pod = Pod([(PM, "go ahead")], hold_until="The deadline passed without a decision.")
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "converged"
    verdict = _verdict(workflow, result["run_id"])
    assert (verdict["how"], verdict["votes"]) == ("rule", {PM: "approve"})
    assert any(
        text
        == f"The deadline passed without a decision. <@{PM}>, your approval or rejection decides it."
        for text in _texts(pod)
    )
    assert len(pod.pulls) == 2


def test_an_asked_owner_who_never_answers_lets_the_discussion_time_out(
    workflow, requester, monkeypatch
):
    _stall_the_clock(monkeypatch)
    _variant(
        workflow,
        POD_AND_OWNER,
        "      decided_by: pod\n      rule: all\n      otherwise: {ask: owner, timeout: 1h}\n",
    )
    pod = Pod([(ENG1, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "timed_out"
    assert sum("The deadline passed without a decision." in text for text in _texts(pod)) == 1
    assert pod.pulls == []


def test_the_requester_cannot_decide_for_a_group_they_are_not_in(workflow, requester, monkeypatch):
    _stall_the_clock(monkeypatch)
    _variant(workflow, POD_AND_OWNER, "      decided_by: pod\n")
    pod = Pod([(PM, "go ahead")])
    agent = PodAgent()

    result = slack_example._run(workflow, pod, agent, monkeypatch)

    assert _status(workflow, result["run_id"]) == "timed_out"
    assert f"Noted. The plan is decided by <@{ENG1}>, <@{ENG2}>." in _texts(pod)


# A reaction gate: an await step decided by people.

LEADS = ["U1", "U2", "U3"]
OWNER = "U0"


class Gate:
    """One await step from the sentry example, decided as declared, driven by
    scripted polls: each poll is the users reacting :+1: and how many seconds
    pass before it, on a clock the test owns."""

    def __init__(self, tmp_path, monkeypatch, decision: str, polls: list[tuple[list[str], int]]):
        from outcomeci.workflow.compiler import compile_workflow

        source = (slack_example.EXAMPLES / "sentry-to-github-pr.outcome.yaml").read_text()
        source = source.replace(
            "reasoning:\n",
            "humans:\n"
            '  leads: {system: slack, type: group, identifier: "@leads"}\n'
            f"  owner: {{system: slack, type: user, identifier: {OWNER}}}\n\nreasoning:\n",
            1,
        ).replace("      timeout: 45m\n", decision + "      timeout: 45m\n", 1)
        config = tmp_path / "wf.outcome.yaml"
        config.write_text(source)
        (tmp_path / ".outcomeci/instructions").mkdir(parents=True)
        for name in ("triage-and-notify.md", "fix-and-open-pr.md"):
            (tmp_path / ".outcomeci/instructions" / name).write_text("x")
        self.compiled = compile_workflow(config)
        self.root, self.step = tmp_path, "approve"
        self.state = {"run_id": "run", "step": "approve"}
        (tmp_path / ".outcomeci/outcomes/run").mkdir(parents=True)
        (tmp_path / ".outcomeci/outcomes/run/humans.json").write_text(
            json.dumps(
                {
                    "leads": {"type": "group", "id": "S1", "users": LEADS},
                    "owner": {"type": "user", "id": OWNER, "users": [OWNER]},
                }
            )
        )
        self.clock = [0.0]
        self.polls = list(polls)
        self.posted: list[str] = []
        self.finished: list[tuple[str, str]] = []
        runtime = slack_example.v1_runtime
        monkeypatch.setattr(runtime.time, "monotonic", lambda: self.clock[0])
        monkeypatch.setattr(runtime.time, "time", lambda: 1_000_000 + self.clock[0])
        monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(runtime, "value", lambda *a: {"channel": "C1", "ts": "100.0"})
        monkeypatch.setattr(runtime, "_covers", lambda *a: [])
        monkeypatch.setattr(runtime, "_poll", self._poll)
        monkeypatch.setattr(
            runtime,
            "_respond",
            lambda executor, api, spec, thread, text, step: self.posted.append(text) or "300.0",
        )
        monkeypatch.setattr(
            local,
            "_finish_interaction",
            lambda *a, **kw: self.finished.append((kw["status"], kw["message"])),
        )

    def _poll(self, *args):
        if not self.polls:
            raise AssertionError("the gate polled after its script ran out")
        users, after = self.polls.pop(0)
        self.clock[0] += after
        return {"output": {"reactions": [{"name": "+1", "count": len(users), "users": users}]}}

    def run(self, **kwargs) -> bool:
        return slack_example.v1_runtime.run_await(
            self.root, self.compiled, self.state, self.step, lambda _: "s", **kwargs
        )

    def wait(self) -> dict:
        return json.loads((self.root / ".outcomeci/outcomes/run/approve/wait.json").read_text())


def test_a_reaction_gate_approves_on_a_majority_of_its_deciders(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: majority\n",
        [(["U9", "U1"], 10), (["U9", "U1", "U3"], 10)],
    )

    assert gate.run()

    assert gate.finished == [("approved", "Approved via :+1: by 2 of 3 (majority)")]
    assert gate.wait()["verdict"] == {
        "by": "by 2 of 3 (majority)",
        "how": "rule",
        "users": ["U1", "U3"],
    }


def test_a_reaction_gate_needs_every_decider_under_rule_all(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n",
        [(["U1", "U2"], 10), (["U1", "U2", "U9"], 10), (["U1", "U2", "U3"], 10)],
    )

    assert gate.run()

    assert gate.finished[-1][1] == "Approved via :+1: by 3 of 3 (all)"


def test_a_reaction_gate_counts_a_number(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: 2\n",
        [(["U3"], 10), (["U3", "U2"], 10)],
    )

    assert gate.run()

    assert gate.finished[-1][1] == "Approved via :+1: by 2 of 3 (2)"


def test_a_reaction_gate_refuses_a_number_larger_than_the_group(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, "      decided_by: leads\n      rule: 4\n", [])

    with pytest.raises(ExecutionError, match="needs 4 of 3"):
        gate.run()


def test_an_override_reaction_approves_whatever_the_count(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n      override: owner\n",
        [(["U1"], 10), (["U1", OWNER], 10)],
    )

    assert gate.run()

    assert gate.finished == [("approved", f"Approved via :+1: by <@{OWNER}>, overriding the tally")]
    assert gate.wait()["verdict"]["how"] == "override"


def test_outsiders_cannot_approve_a_decided_gate(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n",
        [(["U9"], 10), (["U9", "U8"], 3000)],
    )

    assert gate.run() is False

    assert gate.finished == [("expired", "The approval window expired without the signal")]


def test_the_deadline_stops_by_default(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n",
        [(["U1"], 10), (["U1", "U2"], 3000)],
    )

    assert gate.run() is False

    assert gate.finished[-1][0] == "expired"
    assert gate.posted == []


def test_the_deadline_can_continue_a_gate_without_the_signal(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n      otherwise: continue\n",
        [(["U1"], 10), (["U1"], 3000)],
    )

    assert gate.run()

    assert gate.finished == [("approved", "by the deadline, continuing as declared")]
    assert gate.wait()["verdict"]["how"] == "timeout"


def test_the_deadline_can_relax_a_gate_to_the_reactions_given(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n      otherwise: {rule: any}\n",
        [(["U1"], 10), (["U1"], 3000)],
    )

    assert gate.run()

    assert gate.finished == [("approved", "by 1 of 3 (any)")]
    assert gate.wait()["verdict"]["how"] == "relaxed"


def test_a_relaxed_rule_the_reactions_still_miss_expires(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n      otherwise: {rule: majority}\n",
        [(["U1"], 10), (["U1"], 3000)],
    )

    assert gate.run() is False

    assert gate.finished[-1][0] == "expired"


def test_the_deadline_can_ask_the_owner_whose_reaction_then_decides(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n      otherwise: {ask: owner, timeout: 1h}\n",
        [
            (["U1"], 10),
            (["U1"], 3000),  # the deadline passes; the owner is asked
            (["U1", "U2"], 10),  # a second lead is no longer enough on their own
            (["U1", "U2", OWNER], 10),
        ],
    )

    assert gate.run()

    assert gate.posted == [
        f"The deadline passed without a decision. <@{OWNER}>, your approval or rejection "
        "decides it."
    ]
    assert gate.finished == [("approved", "Approved via :+1: by 1 of 1 (any)")]
    assert gate.wait()["verdict"]["users"] == [OWNER]
    assert gate.wait()["deciders"]["asked"] is True


def test_an_asked_owner_who_never_reacts_lets_the_gate_expire(tmp_path, monkeypatch):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: all\n      otherwise: {ask: owner, timeout: 1h}\n",
        [(["U1"], 10), (["U1"], 3000), (["U1", "U2"], 4000)],
    )

    assert gate.run() is False

    assert len(gate.posted) == 1
    assert gate.finished[-1][0] == "expired"


def test_a_parked_gate_carries_its_deciders_and_the_asked_owner_after_the_deadline(
    tmp_path, monkeypatch
):
    gate = Gate(
        tmp_path,
        monkeypatch,
        "      decided_by: leads\n      rule: majority\n      override: owner\n"
        "      otherwise: {ask: owner, timeout: 1h}\n",
        [(["U1"], 10), (["U1"], 10)],
    )

    with pytest.raises(slack_example.v1_runtime.DurableWait) as parked:
        gate.run(durable=True)
    assert parked.value.interaction["deciders"] == LEADS
    assert parked.value.interaction["needed"] == 2
    assert parked.value.interaction["overriders"] == [OWNER]
    first_deadline = parked.value.interaction["expires_at"]

    # The api wakes the run after the deadline: the owner is asked and the
    # gate parks again, now waiting on the owner alone, with a later deadline.
    gate.clock[0] += 3000
    with pytest.raises(slack_example.v1_runtime.DurableWait) as parked:
        gate.run(durable=True)
    assert gate.posted[-1].startswith("The deadline passed without a decision.")
    assert parked.value.interaction["deciders"] == [OWNER]
    assert parked.value.interaction["needed"] == 1
    assert parked.value.interaction["expires_at"] > first_deadline

    # The owner's reaction arrives through the api's captured signal.
    assert gate.run(
        durable=True,
        response={"output": {"reactions": [{"name": "+1", "count": 1, "users": [OWNER]}]}},
    )
    # The owner is the override too, and an override closes it as such.
    assert gate.finished[-1] == (
        "approved",
        f"Approved via :+1: by <@{OWNER}>, overriding the tally",
    )


def test_a_gate_without_deciders_takes_anyone(tmp_path, monkeypatch):
    gate = Gate(tmp_path, monkeypatch, "", [([], 10), (["U9"], 10)])

    assert gate.run()

    assert gate.finished == [("approved", "Approved via :+1: by anyone")]


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
