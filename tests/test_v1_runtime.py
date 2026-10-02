"""Failure paths and trust boundaries of the v1 runtime, on the two examples."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import test_v1_sentry as sentry
import test_v1_slack as slack_example

from outcomeci import local
from outcomeci.config import compile_workflow
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
            executor.execute("github.write", {"method": method, "path": path}, step="fix")
    assert sent == []


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
    assert (state["status"], state["step"]) == ("error", "approve")

    options = local.ExecutionOptions(
        credential_resolver=lambda ref: "xoxb-test-credential",
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

    assert state["completed_steps"] == ["triage", "approve", "fix", "announce"]
    assert "approve" not in agent.prompts
    interaction = (
        workflow / ".outcomeci/outcomes" / state["run_id"] / "interactions/approve/approve.json"
    )
    assert json.loads(interaction.read_text())["status"] == "approved"


def test_an_agent_is_never_run_for_a_runtime_step(workflow, monkeypatch):
    services = sentry.Services()
    sentry._serve(monkeypatch, services)
    config = workflow / sentry.WORKFLOW
    state = {"run_id": "r1", "step": "approve", "status": "queued", "intent": "x"}
    local._write(workflow, state)
    with pytest.raises(ExecutionError, match="driven by the runtime"):
        local._execute(
            workflow,
            config,
            state,
            options=local.ExecutionOptions(
                credential_resolver=lambda ref: "xoxb-test-credential",
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
    assert "announce" in result["completed_steps"]
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
    assert (state["status"], state["step"]) == ("error", "discuss")

    agent = slack_example.Agent()
    monkeypatch.setattr(local, "invoke", agent)
    options = local.ExecutionOptions(
        credential_resolver=lambda ref: "xoxb-test-credential",
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
        set(item) == {"capability", "step", "status", "request"} for item in receipts
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
    assert all(item["step"] == "fix" for review in fix for item in review["receipts"])


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
        broker.execute("github.write", BRANCH, step="fix")
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert [call["status"] for call in journal["calls"].values()] == ["unsent"]

    assert broker.execute("github.write", BRANCH, step="fix")["ok"] is True


def test_a_write_whose_delivery_is_uncertain_is_never_sent_twice(tmp_path, monkeypatch):
    allow = lambda proposal: {  # noqa: E731
        "decision": "allow",
        "proposal_sha256": proposal["proposal_sha256"],
        "reason": "ok",
    }
    broker = _broker(tmp_path, monkeypatch, allow, iter([httpx.Response(502, json={})]))
    with pytest.raises(IntegrationError):
        broker.execute("github.write", BRANCH, step="fix")
    with pytest.raises(IntegrationError, match="delivery is uncertain"):
        broker.execute("github.write", BRANCH, step="fix")


def test_a_failed_read_can_run_again(tmp_path, monkeypatch):
    responses = iter([httpx.Response(404, json={}), httpx.Response(200, json={"ok": True})])
    broker = _broker(tmp_path, monkeypatch, None, responses)
    read = {"method": "GET", "path": "/repos/outcomeci/cli/contents/missing.py"}
    with pytest.raises(IntegrationError):
        broker.execute("github.write", read, step="fix")
    assert broker.execute("github.write", read, step="fix")["ok"] is True


def _github(tmp_path, monkeypatch, handler):
    """A reviewed broker over github.write whose reviewer allows every call."""
    compiled = compile_workflow(sentry.EXAMPLES / sentry.WORKFLOW)
    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)
    executor = IntegrationExecutor(
        compiled,
        resolver=lambda ref: "ghp-test-credential",
        transport=httpx.MockTransport(handler),
        reviewed=True,
    )
    proposals: list = []

    def allow(proposal):
        proposals.append(proposal)
        return {"decision": "allow", "proposal_sha256": proposal["proposal_sha256"], "reason": "ok"}

    grants = [{"capability": "github.write", "args": {"repo": "outcomeci/cli"}, "as": None}]
    broker = PolicyExecutor(
        executor,
        tmp_path,
        {},
        reviewer=allow,
        grants=grants,
        step_policy={"content": "p", "policy": {"runner": "codex"}},
    )
    return broker, proposals


def _commit(text: str) -> dict:
    return {
        "method": "PUT",
        "path": "/repos/outcomeci/cli/contents/app/main.py",
        "body": {
            "message": "log at startup",
            "branch": "slack-feature/log",
            "content": base64.b64encode(text.encode()).decode(),
        },
    }


def test_a_file_commit_is_reviewed_as_a_diff_against_its_branch(tmp_path, monkeypatch):
    current = "import app\n\napp.run()\n# Wed Mar 11 11:36:52 PM CDT 2026\n"
    reads: list = []

    def github(request):
        if request.method == "GET":
            reads.append(request)
            # GitHub wraps a file's base64 content at 60 characters.
            encoded = base64.b64encode(current.encode()).decode()
            wrapped = "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60))
            return httpx.Response(200, json={"content": wrapped, "encoding": "base64"})
        return httpx.Response(200, json={"content": {"path": "app/main.py"}})

    broker, proposals = _github(tmp_path, monkeypatch, github)
    proposed = current.replace("app.run()\n", "print('bear down')\napp.run()\n")
    broker.execute("github.write", _commit(proposed), step="fix")

    (read,) = reads
    assert read.url.path == "/repos/outcomeci/cli/contents/app/main.py"
    assert read.url.params["ref"] == "slack-feature/log"
    diff = proposals[0]["compared"]["diff"]
    assert "+print('bear down')" in diff
    # A line already on the branch is context, not a change.
    assert "# Wed Mar 11" in diff and "+# Wed Mar 11" not in diff
    assert "compared" not in proposals[0]["receipts"][0]


def test_a_new_file_is_reviewed_as_all_added(tmp_path, monkeypatch):
    def github(request):
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(201, json={"content": {"path": "app/main.py"}})

    broker, proposals = _github(tmp_path, monkeypatch, github)
    broker.execute("github.write", _commit("print('hi')\n"), step="fix")

    assert "--- (a new file)" in proposals[0]["compared"]["diff"]
    assert "+print('hi')" in proposals[0]["compared"]["diff"]


def _new_file_github(request):
    if request.method == "GET":
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.Response(201, json={"content": {"path": "app/main.py"}})


def test_a_reviewed_file_write_carries_its_diff_not_the_whole_file(tmp_path, monkeypatch):
    broker, proposals = _github(tmp_path, monkeypatch, _new_file_github)
    commit = _commit("print('hi')\n")
    broker.execute("github.write", commit, step="fix")

    shown = proposals[0]["request"]
    assert shown["body"]["message"] == "log at startup"
    assert shown["body"]["branch"] == "slack-feature/log"
    assert shown["body"]["content"]["omitted"] == "shown as the diff in compared"
    assert commit["body"]["content"] not in json.dumps(proposals[0])
    # The digest still names the full request the broker will send.
    journal = json.loads((tmp_path / "journal.json").read_text())
    (call,) = journal["calls"].values()
    assert proposals[0]["proposal_sha256"] == call["proposal_sha256"]
    assert call["request"]["body"]["content"] == commit["body"]["content"]


def test_receipts_summarize_earlier_bodies_instead_of_repeating_them(tmp_path, monkeypatch):
    broker, proposals = _github(tmp_path, monkeypatch, _new_file_github)
    first = _commit("print('one')\n")
    broker.execute("github.write", first, step="fix")
    broker.execute("github.write", _commit("print('two')\n"), step="fix")

    earlier = proposals[1]["receipts"][0]["request"]
    assert set(earlier["body"]) == {"sha256", "bytes"}
    assert earlier["path"] == first["path"]
    assert first["body"]["content"] not in json.dumps(proposals[1])


def test_a_review_stays_small_however_many_large_files_were_written(tmp_path, monkeypatch):
    broker, proposals = _github(tmp_path, monkeypatch, _new_file_github)
    large = "x = 1\n" * 20_000  # about 120 KB, 160 KB as base64
    for index in range(15):
        commit = _commit(large + f"# {index}\n")
        commit["path"] = f"/repos/outcomeci/cli/contents/app/module_{index}.py"
        broker.execute("github.write", commit, step="fix")

    last = proposals[-1]
    without_diff = {key: value for key, value in last.items() if key != "compared"}
    # Fifteen 160 KB bodies would be 2.4 MB of receipts; summaries keep it small.
    assert len(json.dumps(without_diff)) < 20_000


def test_a_write_that_replaces_no_file_carries_no_diff(tmp_path, monkeypatch):
    broker, proposals = _github(
        tmp_path, monkeypatch, lambda request: httpx.Response(201, json={"ref": "x"})
    )
    broker.execute("github.write", BRANCH, step="fix")

    assert "compared" not in proposals[0]


def _tree(*entries: dict) -> dict:
    return {
        "method": "POST",
        "path": "/repos/outcomeci/cli/git/trees",
        "body": {"base_tree": "abc123", "tree": list(entries)},
    }


def _blob(path: str, text: str) -> dict:
    return {"path": path, "mode": "100644", "type": "blob", "content": text}


def _tree_github(files: dict[str, str], reads: list | None = None):
    """GitHub serving `files` at the default branch, 404 for any other file."""

    def github(request):
        if request.method == "GET":
            if reads is not None:
                reads.append(request)
            name = request.url.path.removeprefix("/repos/outcomeci/cli/contents/")
            if name not in files:
                return httpx.Response(404, json={"message": "Not Found"})
            encoded = base64.b64encode(files[name].encode()).decode()
            return httpx.Response(200, json={"content": encoded, "encoding": "base64"})
        return httpx.Response(201, json={"sha": "tree-sha"})

    return github


def test_a_tree_write_is_reviewed_as_a_diff_of_each_file(tmp_path, monkeypatch):
    reads: list = []
    github = _tree_github({"app/main.py": "app.run()\n", "old.py": "legacy = True\n"}, reads)
    broker, proposals = _github(tmp_path, monkeypatch, github)
    tree = _tree(
        _blob("app/main.py", "print('bear down')\napp.run()\n"),
        _blob("app/new.py", "print('hi')\n"),
        {"path": "old.py", "mode": "100644", "type": "blob", "sha": None},
    )
    broker.execute("github.write", tree, step="fix")

    assert [read.url.path for read in reads] == [
        "/repos/outcomeci/cli/contents/app/main.py",
        "/repos/outcomeci/cli/contents/app/new.py",
        "/repos/outcomeci/cli/contents/old.py",
    ]
    # The tree names only a base tree sha: each file is read at the default branch.
    assert all("ref" not in read.url.params for read in reads)
    compared = proposals[0]["compared"]
    assert compared["path"] == "/repos/outcomeci/cli/git/trees"
    assert compared["files"] == [
        {"path": "app/main.py", "change": "modified"},
        {"path": "app/new.py", "change": "added"},
        {"path": "old.py", "change": "deleted"},
    ]
    diff = compared["diff"]
    assert "+print('bear down')" in diff and "+app.run()" not in diff
    assert "--- (a new file)" in diff and "+print('hi')" in diff
    assert "-legacy = True" in diff and "+++ (deleted)" in diff


def test_a_reviewed_tree_write_carries_its_diffs_not_the_whole_files(tmp_path, monkeypatch):
    broker, proposals = _github(tmp_path, monkeypatch, _tree_github({"a.py": "a = 1\n"}))
    tree = _tree(
        _blob("a.py", "a = 2\n"),
        _blob("b.py", "b = 1\n"),
        {"path": "c.py", "mode": "100644", "type": "blob", "sha": None},
        {"path": "d.py", "mode": "100755", "type": "blob", "sha": "existing-blob"},
    )
    broker.execute("github.write", tree, step="fix")

    shown = proposals[0]["request"]
    assert shown["body"]["base_tree"] == "abc123"
    first, second, deleted, kept = shown["body"]["tree"]
    for entry in (first, second):
        assert entry["content"]["omitted"] == "shown as the diff in compared"
        assert set(entry["content"]) == {"omitted", "sha256", "bytes"}
        assert entry["mode"] == "100644" and entry["type"] == "blob"
    assert [first["path"], second["path"]] == ["a.py", "b.py"]
    assert deleted == tree["body"]["tree"][2] and kept == tree["body"]["tree"][3]
    assert proposals[0]["compared"]["files"][2:] == [
        {"path": "c.py", "change": "deleted", "note": "no current copy"},
        {"path": "d.py", "note": "not compared: the entry carries no new content"},
    ]
    # The digest still names the full request the broker will send.
    journal = json.loads((tmp_path / "journal.json").read_text())
    (call,) = journal["calls"].values()
    assert proposals[0]["proposal_sha256"] == call["proposal_sha256"]
    assert call["request"]["body"] == tree["body"]


def test_a_tree_past_the_entry_limit_notes_the_files_it_did_not_compare(tmp_path, monkeypatch):
    reads: list = []
    broker, proposals = _github(tmp_path, monkeypatch, _tree_github({}, reads))
    limit = sentry.integrations.COMPARED_ENTRIES
    tree = _tree(*(_blob(f"m{index}.py", f"x = {index}\n") for index in range(limit + 3)))
    broker.execute("github.write", tree, step="fix")

    compared = proposals[0]["compared"]
    assert len(reads) == limit and len(compared["files"]) == limit
    assert compared["note"].startswith(f"3 more entries past the first {limit}")
    entries = proposals[0]["request"]["body"]["tree"]
    assert all("omitted" in entry["content"] for entry in entries[:limit])
    # A file the reviewer has no diff for keeps its content.
    assert [entry["content"] for entry in entries[limit:]] == [
        f"x = {index}\n" for index in range(limit, limit + 3)
    ]


def test_a_tree_stops_reading_files_once_its_diffs_reach_the_limit(tmp_path, monkeypatch):
    reads: list = []
    broker, proposals = _github(tmp_path, monkeypatch, _tree_github({}, reads))
    large = "x = 1\n" * 8_000  # about 48 KB of diff, past the limit alone
    tree = _tree(_blob("big.py", large), _blob("small.py", "y = 2\n"))
    broker.execute("github.write", tree, step="fix")

    compared = proposals[0]["compared"]
    assert len(reads) == 1 and compared["truncated"] is True
    assert len(compared["diff"]) == sentry.integrations.DIFF_LIMIT
    assert compared["files"][1] == {
        "path": "small.py",
        "note": "not compared: the diff limit was reached",
    }
    big, small = proposals[0]["request"]["body"]["tree"]
    assert "omitted" in big["content"] and small["content"] == "y = 2\n"


def test_a_tree_entry_outside_the_repository_is_not_read(tmp_path, monkeypatch):
    reads: list = []
    broker, proposals = _github(tmp_path, monkeypatch, _tree_github({}, reads))
    broker.execute("github.write", _tree(_blob("../../other/x.py", "x\n")), step="fix")

    assert reads == []
    assert proposals[0]["compared"]["files"] == [
        {"path": "../../other/x.py", "note": "not compared: not a plain file path"}
    ]
    assert proposals[0]["request"]["body"]["tree"][0]["content"] == "x\n"


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
        "step": "triage",
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
            step="fix",
        )

    assert str(exc.value) == (
        "policy did not approve this exact proposal (deny): "
        "It logs a different message than the plan asks for."
    )


def _search(tmp_path: Path, repo="outcomeci/api") -> PolicyExecutor:
    return _policy(tmp_path, [{"capability": "github.search", "args": {"repo": repo}, "as": None}])


def _query(q=None, path="/search/code") -> dict:
    return {"method": "GET", "path": path, **({"query": {"q": q}} if q is not None else {})}


@pytest.mark.parametrize("repo", [{"owner": "outcomeci", "name": "api"}, "outcomeci/api"])
def test_search_grants_append_the_repo_qualifier_when_absent(tmp_path, repo):
    policy = _search(tmp_path, repo)
    request, _, _ = policy._apply_grants("github.search", _query("parse_config language:python"))
    assert request["query"] == {"q": "parse_config language:python repo:outcomeci/api"}
    assert policy._apply_grants("github.search", _query())[0]["query"] == {
        "q": "repo:outcomeci/api"
    }
    other = {**_query("x"), "query": {"q": "x", "per_page": 50}}
    assert policy._apply_grants("github.search", other)[0]["query"] == {
        "q": "x repo:outcomeci/api",
        "per_page": 50,
    }


@pytest.mark.parametrize(
    "q", ["repo:outcomeci/api parse", "parse REPO:OutComeCI/API", "a  repo:outcomeci/api  b"]
)
def test_search_grants_accept_the_granted_qualifier_in_any_case(tmp_path, q):
    request, _, _ = _search(tmp_path)._apply_grants("github.search", _query(q))
    assert request["query"] == {"q": q}


@pytest.mark.parametrize(
    "q",
    [
        "x repo:outcomeci/api repo:outcomeci/other",
        "x repo:outcomeci/other",
        "x org:evil",
        "x ORG:evil repo:outcomeci/api",
        "x user:someone repo:outcomeci/api",
        "x owner:someone",
        "x -repo:outcomeci/api",
        "x (org:evil) repo:outcomeci/api",
        "x repo: outcomeci/other",
        'x "repo:outcomeci/other"',
    ],
)
def test_search_grants_refuse_any_other_scope_qualifier(tmp_path, q):
    with pytest.raises(IntegrationError, match="q must search only repo:outcomeci/api"):
        _search(tmp_path)._apply_grants("github.search", _query(q))


@pytest.mark.parametrize(
    "q", ["x OR y repo:outcomeci/api", "x NOT repo:outcomeci/api", "(x OR y) repo:outcomeci/api"]
)
def test_search_grants_refuse_operators_that_widen_or_negate_the_scope(tmp_path, q):
    with pytest.raises(IntegrationError, match="q cannot use NOT or OR"):
        _search(tmp_path)._apply_grants("github.search", _query(q))


def test_search_grants_refuse_a_query_hidden_in_the_path_or_not_a_string(tmp_path):
    policy = _search(tmp_path)
    with pytest.raises(IntegrationError, match="q goes in query, not in the path"):
        policy._apply_grants("github.search", _query(path="/search/code?q=org:evil"))
    with pytest.raises(IntegrationError, match="query.q must be a string"):
        policy._apply_grants("github.search", _query(["a", "org:evil"]))


def test_a_search_grant_that_did_not_resolve_refuses_every_call(tmp_path):
    with pytest.raises(IntegrationError, match="repo did not resolve"):
        _search(tmp_path, None)._apply_grants("github.search", _query("x"))


@pytest.mark.parametrize(
    "repo", [{"owner": "o org:evil", "name": "r"}, {"owner": "o", "name": "r)"}, "o /r"]
)
def test_a_search_grant_whose_value_is_not_one_term_refuses_every_call(tmp_path, repo):
    with pytest.raises(IntegrationError, match="repo did not resolve"):
        _search(tmp_path, repo)._apply_grants("github.search", _query("x"))


def test_search_grants_pick_the_repo_the_query_names(tmp_path):
    policy = _policy(
        tmp_path,
        [
            {"capability": "github.search", "args": {"repo": "o/a"}, "as": "a"},
            {"capability": "github.search", "args": {"repo": "o/b"}, "as": "b"},
        ],
    )
    assert policy._apply_grants("github.search", _query("x repo:o/b"))[1] == "b"
    assert policy._apply_grants("github.search", _query("x"))[0]["query"] == {"q": "x repo:o/a"}


def _searching(tmp_path: Path, monkeypatch, sent: list, grant: dict) -> PolicyExecutor:
    """A broker over a compiled v1 workflow whose for_each step searches each
    target's repository, with the grant resolved for one target."""
    import yaml

    from outcomeci import v1_runtime

    document = {
        "apiVersion": "outcomeci.workflow/v1",
        "trigger": "manual",
        "secrets": {"github": "vault:github/pat"},
        "apis": {"github": {"uses": "github", "auth": "secrets.github"}},
        "steps": [
            {
                "find": {
                    "reason": "Find the code.",
                    "for_each": "trigger.targets as target",
                    "can": [grant],
                }
            }
        ],
    }
    path = tmp_path / "search.outcome.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    compiled = compile_workflow(path)
    block = compiled["instructions"]["steps"]["find"]["v1"]
    grants = v1_runtime.resolve_grants(
        tmp_path, {}, block, bound={"target": {"repo": {"owner": "outcomeci", "name": "api"}}}
    )
    monkeypatch.setattr(sentry.integrations, "_safe_destination", lambda url, allow: None)

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"total_count": 1, "items": [{"path": "src/app.py"}]})

    executor = IntegrationExecutor(
        compiled,
        resolver=lambda ref: "ghp-test-credential",
        transport=httpx.MockTransport(handler),
        reviewed=True,
    )
    return PolicyExecutor(
        executor,
        tmp_path / "broker",
        {},
        # Searches are reads, which no reviewer sees; anything else is allowed
        # here so the broker's own checks are what refuse it.
        reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
        grants=grants,
        step_policy={"content": "p", "policy": {"runner": "codex"}},
    )


def test_a_v1_step_searches_only_its_granted_repository(tmp_path, monkeypatch):
    sent: list = []
    broker = _searching(tmp_path, monkeypatch, sent, {"github.search": {"repo": "target.repo"}})
    result = broker.execute("github.search", _query("parse_config"), step="find")
    assert result["ok"] is True
    assert result["output"]["result"]["items"] == [{"path": "src/app.py"}]
    (request,) = sent
    assert request.url.path == "/search/code"
    assert request.url.params.get_list("q") == ["parse_config repo:outcomeci/api"]
    journal = json.loads((tmp_path / "broker" / "journal.json").read_text())
    (call,) = journal["calls"].values()
    assert call["request"]["query"] == {"q": "parse_config repo:outcomeci/api"}

    for inputs, message in [
        (_query("x org:outcomeci"), "q must search only repo:outcomeci/api"),
        (_query("x", path="/search/commits"), "search covers code only"),
        (_query("x", path="/repos/outcomeci/api/contents/a.py"), "search covers code only"),
        ({**_query("x"), "method": "POST"}, "input is invalid"),
    ]:
        with pytest.raises(IntegrationError, match=message):
            broker.execute("github.search", inputs, step="find")
    assert len(sent) == 1
    denied = [
        item
        for item in journal_events(tmp_path / "broker")
        if item["event_type"] == "permission.denied"
    ]
    assert any("q must search only" in item["message"] for item in denied)


def test_an_unscoped_search_grant_searches_anything_its_token_can(tmp_path, monkeypatch):
    sent: list = []
    broker = _searching(tmp_path, monkeypatch, sent, "github.search")
    broker.execute("github.search", _query("x org:outcomeci"), step="find")
    assert sent[0].url.params["q"] == "x org:outcomeci"
    with pytest.raises(IntegrationError, match="search covers code only"):
        broker.execute("github.search", _query("x", path="/search/issues"), step="find")


def journal_events(directory: Path) -> list[dict]:
    return json.loads((directory / "journal.json").read_text()).get("events", [])
