"""Failure paths and trust boundaries of the v1 runtime, on the two examples."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import test_v1_sentry as sentry
import test_v1_slack as slack_example
import yaml

from outcomeci import local
from outcomeci.config import ConfigError, compile_workflow
from outcomeci.integrations import IntegrationError, IntegrationExecutor
from outcomeci.policy import PolicyExecutor
from outcomeci.process import ExecutionError

workflow = sentry.workflow


def _slack_root(tmp_path: Path, monkeypatch) -> Path:
    import shutil

    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)
    monkeypatch.setattr(sentry.v1_runtime.time, "sleep", lambda seconds: None)
    return tmp_path / "wf"


def _policy(tmp_path: Path, grants: list[dict]) -> PolicyExecutor:
    compiled = compile_workflow(sentry.EXAMPLES / sentry.WORKFLOW)
    return PolicyExecutor(IntegrationExecutor(compiled), tmp_path, {}, grants=grants)


def test_field_grants_fill_in_and_refuse_other_values(tmp_path):
    policy = _policy(
        tmp_path, [{"capability": "slack.post", "args": {"channel": "sentry"}, "as": None}]
    )
    assert policy._apply_grants("slack.post", {"text": "hi"}) == (
        {"text": "hi", "channel": "sentry"},
        None,
        [],
    )
    assert (
        policy._apply_grants("slack.post", {"text": "hi", "channel": "#sentry"})[0]["channel"]
        == "#sentry"
    )
    with pytest.raises(IntegrationError, match="channel must be sentry"):
        policy._apply_grants("slack.post", {"text": "hi", "channel": "general"})


def test_a_grant_that_did_not_resolve_refuses_every_call(tmp_path):
    policy = _policy(tmp_path, [{"capability": "github.write", "args": {"repo": None}, "as": None}])
    with pytest.raises(IntegrationError, match="repo did not resolve"):
        policy._apply_grants("github.write", {"method": "POST", "path": "/repos/o/r/pulls"})


@pytest.mark.parametrize("repo", [{"owner": "o", "name": "r"}, "o/r"])
def test_path_grants_accept_both_repo_forms(tmp_path, repo):
    policy = _policy(tmp_path, [{"capability": "github.write", "args": {"repo": repo}, "as": None}])
    assert policy._apply_grants("github.write", {"method": "POST", "path": "/repos/o/r/pulls"})
    with pytest.raises(IntegrationError, match="path must be under /repos/o/r"):
        policy._apply_grants("github.write", {"method": "POST", "path": "/repos/o/other/pulls"})


def test_the_matching_grant_names_the_call(tmp_path):
    policy = _policy(
        tmp_path,
        [
            {"capability": "slack.post", "args": {"channel": "build"}, "as": "plan_post"},
            {"capability": "slack.post", "args": {"channel": "sentry"}, "as": "alert"},
        ],
    )
    assert policy._apply_grants("slack.post", {"text": "x", "channel": "sentry"})[1] == "alert"
    assert policy._apply_grants("slack.post", {"text": "x", "channel": "build"})[1] == "plan_post"


def test_github_write_refuses_merges_and_settings_before_sending(monkeypatch):
    sent = []

    def transport(request):
        sent.append(request)
        return httpx.Response(200, json={})

    compiled = compile_workflow(sentry.EXAMPLES / sentry.WORKFLOW)
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda ref: "ghp-test-credential",
        transport=httpx.MockTransport(transport),
        reviewed=True,
    )
    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)
    for method, path in [("PUT", "/repos/o/r/pulls/1/merge"), ("PATCH", "/repos/o/r")]:
        with pytest.raises(IntegrationError, match="does not allow"):
            executor.execute("github.write", {"method": method, "path": path}, phase="fix")
    assert sent == []


def test_v1alpha1_files_keep_their_contract(tmp_path):
    base = yaml.safe_load(
        (Path(__file__).parent.parent / "examples/integration-package/outcome.yml").read_text()
    )
    for mutate, message in [
        (
            lambda doc: doc["spec"]["agents"]["phases"]["intake"].update(
                instructions={"content": "x"}
            ),
            "inline instructions",
        ),
    ]:
        doc = json.loads(json.dumps(base))
        mutate(doc)
        path = tmp_path / "outcome.yml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        for name in ("instructions.md", "customer.integration.yml"):
            (tmp_path / name).write_text(
                (Path(__file__).parent.parent / "examples/integration-package" / name).read_text()
            )
        with pytest.raises(ConfigError, match=message):
            compile_workflow(path)


class FlakySlack(sentry.Services):
    """Fails the first reactions reads with a status, then behaves."""

    def __init__(self, status: int, failures: int = 1):
        super().__init__()
        self.status, self.failures = status, failures

    def __call__(self, request):
        if request.url.path == "/api/reactions.get" and self.failures:
            self.failures -= 1
            self.requests.append(request)
            return httpx.Response(self.status, json={"ok": False})
        return super().__call__(request)


def test_a_transient_poll_error_keeps_waiting(workflow, monkeypatch):
    services = FlakySlack(503)
    sentry._serve(monkeypatch, services)
    monkeypatch.setattr(sentry.v1_runtime, "POLL_BACKOFF_SECONDS", 0)

    result = sentry._run(workflow, sentry.Agent(), monkeypatch)

    assert result["status"] == "completed"
    assert len(services.sent("slack.com", "/api/reactions.get")) == 2


def test_a_failed_await_is_retried_by_the_runtime_never_by_an_agent(workflow, monkeypatch):
    services = FlakySlack(400)
    sentry._serve(monkeypatch, services)
    agent = sentry.Agent()

    with pytest.raises(ExecutionError):
        sentry._run(workflow, agent, monkeypatch)
    run = next((workflow / ".outcomeci/outcomes").glob("*/run.json"))
    state = json.loads(run.read_text())
    assert (state["status"], state["phase"]) == ("error", "approve")

    options = local.ExecutionOptions(
        credential_resolver=lambda ref: "xoxb-test-credential",
        execution_backend="outcomeci",
        policy_reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
    )
    config = workflow / sentry.WORKFLOW
    state = local.retry(workflow, config, state["run_id"], options=options)
    while state["status"] == "awaiting_confirmation":
        state = local.continue_run(workflow, config, state["run_id"], approve=True, options=options)

    assert state["completed_phases"] == ["triage", "approve", "fix", "announce"]
    assert "approve" not in agent.prompts
    interaction = (
        workflow / ".outcomeci/outcomes" / state["run_id"] / "interactions/approve/approve.json"
    )
    assert json.loads(interaction.read_text())["status"] == "approved"


def test_an_agent_is_never_run_for_a_runtime_step(workflow, monkeypatch):
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    config = workflow / sentry.WORKFLOW
    state = {"run_id": "r1", "phase": "approve", "status": "queued", "intent": "x"}
    local._write(workflow, state)
    with pytest.raises(ExecutionError, match="driven by the runtime"):
        local._execute(
            workflow,
            config,
            state,
            options=local.ExecutionOptions(
                credential_resolver=lambda ref: "xoxb-test-credential",
                execution_backend="outcomeci",
            ),
        )


class NoPR(sentry.Agent):
    """A fix step that finds no confident fix and says why."""

    def __call__(self, runner, model, prompt, root, timeout, **kwargs):
        context = sentry._context(prompt)
        if context["step"] == "fix":
            Path(context["returns"]["path"]).write_text(
                json.dumps({"reason": "unclear root cause"})
            )
            self.prompts["fix"] = context
            return "no fix"
        return super().__call__(runner, model, prompt, root, timeout, **kwargs)


def test_a_fix_without_a_pull_request_announces_why(workflow, monkeypatch):
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    agent = NoPR()

    result = sentry._run(workflow, agent, monkeypatch)

    assert result["status"] == "completed"
    assert "announce" in result["completed_phases"]
    inputs = {item["name"]: item["value"] for item in agent.prompts["announce"]["inputs"]}
    assert inputs["fix"] == {"reason": "unclear root cause"}


class Scripted(slack_example.Agent):
    """Turn answers come from a script; a None entry writes an invalid answer."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)

    def _turn(self, prompt):
        if self.answers and self.answers[0] is None:
            self.answers.pop(0)
            path = Path(slack_example.re.search(r"to (/\S+\.json)\.", prompt).group(1))
            path.write_text("not json", encoding="utf-8")
            self.turns.append("<invalid>")
            return "bad"
        if self.answers:
            self.answers.pop(0)
        return super()._turn(prompt)


def test_an_invalid_turn_is_repaired_once(tmp_path, monkeypatch):
    root = _slack_root(tmp_path, monkeypatch)
    slack = slack_example.Slack(["go ahead"])
    agent = Scripted([None, "ok"])

    result = slack_example._run(root, slack, agent, monkeypatch)

    assert result["status"] == "completed"
    assert agent.turns == ["<invalid>", "go ahead"]


def test_a_reply_left_unanswered_by_a_crash_is_answered_on_retry(tmp_path, monkeypatch):
    root = _slack_root(tmp_path, monkeypatch)
    slack = slack_example.Slack(["go ahead"])
    with pytest.raises(ExecutionError, match="converse turn"):
        slack_example._run(root, slack, Scripted([None, None]), monkeypatch)
    state = json.loads(next((root / ".outcomeci/outcomes").glob("*/run.json")).read_text())
    assert (state["status"], state["phase"]) == ("error", "discuss")

    agent = slack_example.Agent()
    monkeypatch.setattr(local, "invoke", agent)
    options = local.ExecutionOptions(
        credential_resolver=lambda ref: "xoxb-test-credential",
        execution_backend="outcomeci",
        policy_reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
    )
    config = root / slack_example.WORKFLOW
    state = local.retry(root, config, state["run_id"], options=options)
    while state["status"] == "awaiting_confirmation":
        state = local.continue_run(root, config, state["run_id"], approve=True, options=options)

    assert agent.turns == ["go ahead"]
    assert state["status"] == "completed"
    consultation = slack_example._consultation(root, state["run_id"])
    assert consultation["status"] == "converged"


class Stranger(slack_example.Slack):
    """Someone other than the requester says go ahead first."""

    def __call__(self, request):
        if (
            request.url.path == "/api/conversations.replies"
            and self.humans
            and self.humans[0] == "go ahead"
            and not getattr(self, "interrupted", False)
            and self.thread[-1].get("bot_id")
        ):
            self.interrupted = True
            self.thread.append({"ts": self._ts(), "text": "go ahead", "user": "U2"})
            self.thread.append({"ts": self._ts(), "text": self.humans.pop(0), "user": "U1"})
        return super().__call__(request)


def test_only_the_requester_can_steer_or_approve_the_plan(tmp_path, monkeypatch):
    root = _slack_root(tmp_path, monkeypatch)
    slack = Stranger(["go ahead"])
    agent = slack_example.Agent()

    result = slack_example._run(root, slack, agent, monkeypatch)

    assert result["status"] == "completed"
    assert agent.turns == ["go ahead"]
    humans = [
        turn
        for turn in slack_example._consultation(root, result["run_id"])["turns"]
        if turn["from"] == "human"
    ]
    assert len(humans) == 1


class Verbose(slack_example.Agent):
    def _turn(self, prompt):
        path = Path(slack_example.re.search(r"to (/\S+\.json)\.", prompt).group(1))
        plan = json.loads(
            slack_example.re.search(r"Current plan \(version \d+\): (.+)\n", prompt).group(1)
        )
        long_text = "\n".join(f"line {index} " + "x" * 90 for index in range(100))
        path.write_text(json.dumps({"status": "converged", "plan": plan, "message": long_text}))
        return "done"


def test_long_answers_are_split_to_fit_one_post_each(tmp_path, monkeypatch):
    root = _slack_root(tmp_path, monkeypatch)
    slack = slack_example.Slack(["go ahead"])

    slack_example._run(root, slack, Verbose(), monkeypatch)

    answers = [post for post in slack.posts if post["text"].startswith("line ")]
    assert len(answers) == 3
    assert all(len(post["text"]) <= 3900 for post in answers)
    assert "".join(post["text"] for post in answers).count("line ") == 100


class Partial(slack_example.Agent):
    """The api repository does not match the plan, so its run opens no PR."""

    def __call__(self, runner, model, prompt, root, timeout, **kwargs):
        if "You are taking one turn in a discussion" in prompt:
            return self._turn(prompt)
        context = json.loads(prompt.rsplit("\n", 1)[-1])
        inputs = {item["name"]: item["value"] for item in context["inputs"]}
        if context["step"] == "implement" and inputs["target"]["repo"]["name"] == "api":
            Path(context["returns"]["path"]).write_text(json.dumps({"reason": "no such module"}))
            return "no pr"
        return super().__call__(runner, model, prompt, root, timeout, **kwargs)


def test_gathered_outputs_keep_one_entry_per_item(tmp_path, monkeypatch):
    root = _slack_root(tmp_path, monkeypatch)
    slack = slack_example.Slack(["go ahead"])

    result = slack_example._run(root, slack, Partial(), monkeypatch)

    gathered = json.loads(
        (root / ".outcomeci/outcomes" / result["run_id"] / "implement/outputs.json").read_text()
    )
    assert gathered["pr"][0]["number"] == 1
    assert gathered["pr"][1] is None
    assert gathered["reason"] == [None, "no such module"]


def test_a_step_policy_review_inside_the_container_uses_no_nested_sandbox(tmp_path, monkeypatch):
    from outcomeci import policy as policy_module

    seen = {}

    def invoke(runner, model, prompt, workspace, timeout, **kwargs):
        seen.update(kwargs)
        return '{"decision": "allow", "proposal_sha256": "x", "reason": "ok"}'

    monkeypatch.setattr(policy_module, "invoke", invoke)
    compiled = compile_workflow(sentry.EXAMPLES / sentry.WORKFLOW)
    reviewer = PolicyExecutor(
        IntegrationExecutor(compiled), tmp_path, {}, grants=[], container_isolated=True
    )
    reviewer._review({"policy": {"content": "policy", "policy": {"runner": "codex"}}})

    assert seen["container_isolated"] is True
    assert seen["read_only"] is True


def test_a_review_sees_earlier_requests_but_not_their_response_bodies(workflow, monkeypatch):
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    reviews: list = []

    sentry._run(workflow, sentry.Agent(), monkeypatch, reviews)

    receipts = reviews[0]["receipts"]
    assert receipts and all(
        set(item) == {"capability", "phase", "status", "request"} for item in receipts
    )


def test_a_review_sees_only_its_own_steps_requests(workflow, monkeypatch):
    """triage's Slack post is not fix's to follow; only fix's own calls are receipts."""
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    reviews: list = []

    sentry._run(workflow, sentry.Agent(), monkeypatch, reviews)

    fix = [
        review
        for review in reviews
        if review["policy"]["content"].startswith("Step policy for fix:")
    ]
    assert fix
    assert all(item["phase"] == "fix" for review in fix for item in review["receipts"])


def _broker(tmp_path, monkeypatch, reviewer, responses):
    compiled = compile_workflow(sentry.EXAMPLES / sentry.WORKFLOW)
    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda ref: "ghp-test-credential",
        transport=httpx.MockTransport(lambda request: next(responses)),
        reviewed=True,
    )
    grants = [{"capability": "github.write", "args": {"repo": "outcomeci/cli"}, "as": None}]
    return PolicyExecutor(
        executor,
        tmp_path,
        {},
        reviewer=reviewer,
        grants=grants,
        step_policy={"content": "p", "policy": {"runner": "codex"}},
    )


BRANCH = {
    "method": "POST",
    "path": "/repos/outcomeci/cli/git/refs",
    "body": {"ref": "refs/heads/x"},
}


def test_a_call_whose_review_failed_was_never_sent_and_can_run_again(tmp_path, monkeypatch):
    attempts = iter([RuntimeError("advisor crashed"), None])

    def reviewer(proposal):
        failure = next(attempts)
        if failure:
            raise failure
        return {"decision": "allow", "proposal_sha256": proposal["proposal_sha256"], "reason": "ok"}

    broker = _broker(
        tmp_path, monkeypatch, reviewer, iter([httpx.Response(201, json={"ref": "x"})])
    )
    with pytest.raises(RuntimeError):
        broker.execute("github.write", BRANCH, phase="fix")
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert [call["status"] for call in journal["calls"].values()] == ["unsent"]

    assert broker.execute("github.write", BRANCH, phase="fix")["ok"] is True


def test_a_write_whose_delivery_is_uncertain_is_never_sent_twice(tmp_path, monkeypatch):
    allow = lambda proposal: {  # noqa: E731
        "decision": "allow",
        "proposal_sha256": proposal["proposal_sha256"],
        "reason": "ok",
    }
    broker = _broker(tmp_path, monkeypatch, allow, iter([httpx.Response(502, json={})]))
    with pytest.raises(IntegrationError):
        broker.execute("github.write", BRANCH, phase="fix")
    with pytest.raises(IntegrationError, match="delivery is uncertain"):
        broker.execute("github.write", BRANCH, phase="fix")


def test_a_failed_read_can_run_again(tmp_path, monkeypatch):
    responses = iter([httpx.Response(404, json={}), httpx.Response(200, json={"ok": True})])
    broker = _broker(tmp_path, monkeypatch, None, responses)
    read = {"method": "GET", "path": "/repos/outcomeci/cli/contents/missing.py"}
    with pytest.raises(IntegrationError):
        broker.execute("github.write", read, phase="fix")
    assert broker.execute("github.write", read, phase="fix")["ok"] is True


def _journal_with(tmp_path: Path, calls: dict, events: list) -> Path:
    journal = tmp_path / ".outcomeci" / ".broker" / "run-1" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"calls": calls, "events": events}), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("status", "events", "expected"),
    [
        (
            "uncertain",
            [{"proposal_sha256": "p1", "detail": "provider rejected the request (not_in_channel)"}],
            "step triage's slack.post failed: provider rejected the request (not_in_channel)",
        ),
        (
            "denied",
            [{"proposal_sha256": "p1", "reason": "channel must be sentry"}],
            "step triage's slack.post was denied: channel must be sentry",
        ),
    ],
)
def test_a_waiting_step_says_why_its_message_is_missing(tmp_path, status, events, expected):
    from outcomeci.v1_runtime import missing_call

    call = {
        "phase": "triage",
        "capability": "slack.post",
        "sequence": 1,
        "status": status,
        "proposal_sha256": "p1",
    }
    root = _journal_with(tmp_path, {"p1": call}, events)

    assert missing_call(root, "run-1", "triage.calls.slack.post") == expected


def test_a_waiting_step_says_when_its_message_was_never_posted(tmp_path):
    from outcomeci.v1_runtime import missing_call

    root = _journal_with(tmp_path, {}, [])

    assert missing_call(root, "run-1", "triage.calls.slack.post") == (
        "step triage never called slack.post"
    )


def test_a_denied_change_tells_the_agent_the_reviewers_reason(tmp_path, monkeypatch):
    compiled = compile_workflow(sentry.EXAMPLES / sentry.WORKFLOW)
    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda ref: "ghp-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        reviewed=True,
    )
    policy = PolicyExecutor(
        executor,
        tmp_path,
        {},
        reviewer=lambda proposal: {
            "decision": "deny",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "It logs a different message than the plan asks for.",
        },
        grants=[
            {
                "capability": "github.write",
                "args": {"repo": {"owner": "outcomeci", "name": "cli"}},
                "as": None,
            }
        ],
        step_policy={"content": "One PR.", "policy": {}},
    )

    with pytest.raises(IntegrationError) as exc:
        policy.execute(
            "github.write",
            {"method": "POST", "path": "/repos/outcomeci/cli/pulls"},
            phase="fix",
        )

    assert str(exc.value) == (
        "policy did not approve this exact proposal (deny): "
        "It logs a different message than the plan asks for."
    )
