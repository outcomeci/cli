"""Local checkouts of the repositories a step is granted: which, how, and what the agent hears."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import test_v1_slack as slack_example

from outcomeci.broker import executor as integrations
from outcomeci.runtime import checkouts
from outcomeci.runtime import container as run_container
from outcomeci.runtime import engine as local
from outcomeci.runtime import steps as v1_runtime
from outcomeci.workflow.compiler import compile_workflow

pytestmark = pytest.mark.repository_checkouts
TOKEN = "ghp_checkoutTestToken0123456789"
POLICY_EVENT_TYPES = {
    "integration.proposed",
    "permission.reviewed",
    "permission.denied",
    "integration.started",
    "integration.completed",
    "integration.failed",
}


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _remote(base: Path, owner: str, name: str, files: dict[str, str]) -> str:
    """A local repository standing in for github.com/<owner>/<name>; its head commit."""
    path = base / owner / name
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    for relative, content in files.items():
        (path / relative).write_text(content, encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-q", "-m", "initial", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


@pytest.fixture
def remotes(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "remotes"
    base.mkdir()
    monkeypatch.setattr(checkouts, "clone_url", lambda owner, name: (base / owner / name).as_uri())
    return base


def _compiled():
    return compile_workflow(slack_example.EXAMPLES / slack_example.WORKFLOW)


def _repository(owner: str = "outcomeci", name: str = "cli") -> checkouts.Repository:
    return checkouts.granted(
        _compiled(),
        [{"capability": "github.write", "args": {"repo": f"{owner}/{name}"}, "as": None}],
    )[0]


def test_only_grants_naming_a_repository_get_a_checkout():
    compiled = _compiled()
    found = checkouts.granted(
        compiled,
        [
            {"capability": "github.write", "args": {}, "as": None},
            {"capability": "github.write", "args": {"repo": None}, "as": None},
            {"capability": "slack.post", "args": {"channel": "C1"}, "as": None},
            {
                "capability": "github.write",
                "args": {"repo": {"owner": "outcomeci", "name": "cli"}},
                "as": None,
            },
            {"capability": "github.write", "args": {"repo": "outcomeci/CLI"}, "as": "again"},
            {"capability": "github.write", "args": {"repo": "outcomeci/.."}, "as": "escape"},
        ],
    )

    assert [(item.full_name, item.capabilities) for item in found] == [
        ("outcomeci/cli", ("github.write",))
    ]


def test_an_unrestricted_grant_gets_no_checkout(tmp_path, remotes):
    _remote(remotes, "outcomeci", "cli", {"README.md": "hi\n"})

    records = checkouts.prepare(
        tmp_path,
        _compiled(),
        [{"capability": "github.write", "args": {}, "as": None}],
        lambda reference: TOKEN,
        step="implement",
    )

    assert records == []
    assert not checkouts.directory(tmp_path).exists()


def test_the_clone_is_authenticated_but_keeps_no_credential(tmp_path, remotes, monkeypatch):
    commit = _remote(remotes, "outcomeci", "cli", {"README.md": "hi\n"})
    environments: list[dict] = []
    real = subprocess.Popen

    def popen(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            environments.append(kwargs["env"])
        return real(argv, **kwargs)

    monkeypatch.setattr(checkouts.subprocess, "Popen", popen)
    events: list[dict] = []

    records = checkouts.prepare(
        tmp_path,
        _compiled(),
        [{"capability": "github.write", "args": {"repo": "outcomeci/cli"}, "as": None}],
        lambda reference: TOKEN,
        step="implement",
        event_sink=events.append,
    )

    checkout = checkouts.directory(tmp_path) / "outcomeci" / "cli"
    assert records == [
        {
            "repo": "outcomeci/cli",
            "ref": None,
            "capabilities": ["github.write"],
            "commit": commit,
            "bytes": records[0]["bytes"],
            "path": str(checkout),
        }
    ]
    assert (checkout / "README.md").read_text() == "hi\n"
    # The credential reached git through its environment only, scoped to github.com.
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    (env,) = environments
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_1"] == f"Authorization: basic {basic}"
    for path in (checkout / ".git").rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert TOKEN.encode() not in content and basic.encode() not in content, path
    assert _git("remote", "get-url", "origin", cwd=checkout) == (
        "https://github.com/outcomeci/cli.git"
    )
    assert _git("rev-parse", "--is-shallow-repository", cwd=checkout) == "true"
    (completed,) = events
    assert completed["event_type"] == "integration.completed"
    assert completed["capability"] == "github.write"
    assert json.loads(completed["detail"]) == {
        "repo": "outcomeci/cli",
        "commit": commit,
        "bytes": records[0]["bytes"],
    }
    assert TOKEN not in json.dumps(events) and basic not in json.dumps(events)


def test_a_checkout_that_kept_a_credential_is_discarded(tmp_path, remotes, monkeypatch):
    _remote(remotes, "outcomeci", "cli", {"README.md": "hi\n"})
    real = checkouts._git

    def leaky(argv, cwd):
        if argv[0] == "remote":
            (cwd / ".git" / "FETCH_HEAD").write_text(f"https://x-access-token:{TOKEN}@github.com")
        return real(argv, cwd)

    monkeypatch.setattr(checkouts, "_git", leaky)
    destination = tmp_path / "cli"

    with pytest.raises(checkouts.CheckoutError, match="kept a credential"):
        checkouts.clone(_repository(), destination, "Authorization: basic x", secrets=[TOKEN])
    assert not destination.exists()


def test_a_repository_over_the_size_cap_is_not_checked_out(tmp_path, remotes):
    _remote(remotes, "outcomeci", "cli", {"big.txt": "x" * 100_000})
    destination = tmp_path / "cli"

    with pytest.raises(checkouts.CheckoutError, match="larger than 1000 bytes"):
        checkouts.clone(_repository(), destination, None, secrets=[], max_bytes=1000)
    assert not destination.exists()


class Inspecting(slack_example.Agent):
    """Records what each implement agent was told and could read."""

    def __init__(self):
        super().__init__()
        self.seen: list[dict] = []

    def __call__(self, runner, model, prompt, root, timeout, **kwargs):
        if "You are taking one turn in a discussion" not in prompt:
            context = json.loads(prompt.rsplit("\n", 1)[-1])
            if context["step"] == "implement":
                base = checkouts.directory(Path(root))
                self.seen.append(
                    {
                        "prompt": prompt,
                        "present": sorted(str(path.relative_to(base)) for path in base.glob("*/*")),
                    }
                )
        return super().__call__(runner, model, prompt, root, timeout, **kwargs)


def _run(root: Path, slack, agent, monkeypatch, events: list) -> dict:
    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(slack)
        return real(*args, **kwargs)

    monkeypatch.setattr(integrations.httpx, "Client", client)
    monkeypatch.setattr(local, "invoke", agent)
    config = root / slack_example.WORKFLOW
    options = local.ExecutionOptions(
        credential_resolver=lambda reference: TOKEN,
        event_sink=events.append,
        policy_reviewer=lambda proposal: {
            "decision": "allow",
            "proposal_sha256": proposal["proposal_sha256"],
            "reason": "ok",
        },
    )
    return run_container.execute(
        root,
        config,
        compile_workflow(config),
        "webhook",
        slack_example._payload(),
        options,
        auto_continue=True,
    )


def test_each_item_reads_only_its_own_repository_locally(
    slack_example_workflow, remotes, monkeypatch
):
    cli = _remote(remotes, "outcomeci", "cli", {"cli.py": "print('cli')\n"})
    api = _remote(remotes, "outcomeci", "api", {"api.py": "print('api')\n"})
    agent = Inspecting()
    events: list[dict] = []

    result = _run(
        slack_example_workflow, slack_example.Slack(["go ahead"]), agent, monkeypatch, events
    )

    assert result["status"] == "completed"
    base = checkouts.directory(slack_example_workflow)
    assert [seen["present"] for seen in agent.seen] == [["outcomeci/cli"], ["outcomeci/api"]]
    for seen, name, commit in zip(agent.seen, ["cli", "api"], [cli, api], strict=True):
        assert f"- outcomeci/{name}: {base / 'outcomeci' / name} " in seen["prompt"]
        assert (
            f"(the default branch at commit {commit}; changes through github.write)"
            in (seen["prompt"])
        )
        assert "local reads and searches are free" in seen["prompt"]
    # Checkouts are gone once their agents finish, and never among the artifacts.
    assert not base.exists()
    outcome = slack_example_workflow / ".outcomeci/outcomes" / result["run_id"]
    assert not list(outcome.rglob("cli.py"))
    assert [
        (item["item"], item["repo"], item["commit"]) for item in result["repository_checkouts"]
    ] == [(0, "outcomeci/cli", cli), (1, "outcomeci/api", api)]
    # The final manifest names every repository the run read and its commit,
    # even though the last step (the Slack announcement) checked nothing out.
    manifest = json.loads((outcome / "manifest.json").read_text())
    assert manifest["step"] != "implement"
    assert {
        key: value
        for key, value in manifest["repository_base_commits"].items()
        if key.startswith("outcomeci/")
    } == {"outcomeci/cli": cli, "outcomeci/api": api}
    checkout_events = [item for item in events if item["message"].startswith("Checked out")]
    assert len(checkout_events) == 2
    assert {item["event_type"] for item in events} <= POLICY_EVENT_TYPES
    # A clone is not an API request: the broker journal holds only the agents' calls.
    journal = json.loads(
        (
            slack_example_workflow / ".outcomeci/.broker" / result["run_id"] / "journal.json"
        ).read_text()
    )
    github = [call for call in journal["calls"].values() if call["capability"] == "github.write"]
    assert sorted(call["request"]["path"] for call in github) == [
        "/repos/outcomeci/api/pulls",
        "/repos/outcomeci/cli/pulls",
    ]


def test_a_failed_checkout_is_a_warning_and_the_run_continues(
    slack_example_workflow, remotes, monkeypatch
):
    cli = _remote(remotes, "outcomeci", "cli", {"cli.py": "print('cli')\n"})
    agent = Inspecting()
    events: list[dict] = []

    result = _run(
        slack_example_workflow, slack_example.Slack(["go ahead"]), agent, monkeypatch, events
    )

    assert result["status"] == "completed"
    assert result["completed_steps"] == ["draft", "discuss", "implement", "announce"]
    assert f"commit {cli}" in agent.seen[0]["prompt"]
    assert "Local checkouts" not in agent.seen[1]["prompt"]
    (warning,) = [item for item in events if item["event_type"] == "integration.failed"]
    assert warning["level"] == "warning"
    assert "outcomeci/api" in warning["message"]
    assert TOKEN not in json.dumps(events)
    failed = result["repository_checkouts"][1]
    assert failed["repo"] == "outcomeci/api" and "error" in failed and "path" not in failed


@pytest.fixture
def slack_example_workflow(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(slack_example.EXAMPLES, tmp_path / "wf")
    monkeypatch.setattr(
        local, "_transcripts", lambda *a, **k: {"usage_records": 0, "files": [], "usage": []}
    )
    monkeypatch.setattr(integrations, "_safe_destination", lambda url, allow_private: None)
    monkeypatch.setattr(v1_runtime.time, "sleep", lambda seconds: None)
    return tmp_path / "wf"
