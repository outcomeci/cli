import json
import shutil

import httpx
import pytest
import test_v1_slack as example
from test_v1_runtime import _slack_root

from outcomeci.broker import executor as integrations
from outcomeci.cloud_runner import checkpoint
from outcomeci.cloud_runner.models import ContractError
from outcomeci.runtime import engine as local
from outcomeci.runtime import steps as v1_runtime


@pytest.mark.parametrize(
    "wake", ["reply", "polled_reply", "polled_before_expiry", "timeout", "changed_workflow"]
)
def test_wait_restores_exact_run_without_reposting_or_replaying_draft(tmp_path, monkeypatch, wake):
    root = _slack_root(tmp_path, monkeypatch)
    config = root / example.WORKFLOW
    slack, agent = example.Slack([]), example.Agent()
    real = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(slack)}),
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
    first = local.trigger(root, config, "webhook", example._payload(), options=options)
    with pytest.raises(v1_runtime.DurableWait) as waiting:
        local.continue_run(root, config, first["run_id"], approve=True, options=options)
    assert waiting.value.interaction["kind"] == "slack_conversation"
    before = len(slack.posts)
    snapshot = checkpoint.capture(root, first["run_id"])
    fresh = tmp_path / "fresh"
    shutil.copytree(example.EXAMPLES, fresh)
    (fresh / example.WORKFLOW).write_text(config.read_text())
    checkpoint.restore(fresh, first["run_id"], snapshot)
    with pytest.raises(v1_runtime.DurableWait) as again:
        local.resume_wait(fresh, fresh / example.WORKFLOW, first["run_id"], options=options)
    assert len(slack.posts) == before
    assert again.value.interaction["expires_at"] == waiting.value.interaction["expires_at"]
    if wake == "changed_workflow":
        config = fresh / example.WORKFLOW
        config.write_text(
            config.read_text().replace("Find every repository", "Carefully find every repository")
        )
        with pytest.raises(local.ExecutionError, match="workflow changed"):
            local.resume_wait(fresh, config, first["run_id"], options=options)
        return
    if wake in {"timeout", "polled_before_expiry"}:
        path = fresh / ".outcomeci/outcomes" / first["run_id"] / "discuss/consultation.json"
        consultation = json.loads(path.read_text())
        consultation["expires_at"] = 1
        path.write_text(json.dumps(consultation))
    if wake in {"polled_reply", "polled_before_expiry"}:
        options.resume_response = {
            "reason": "reply",
            "output": {
                "messages": [{"ts": "100.0"}, {"ts": "999.0", "text": "go ahead", "user": "U1"}]
            },
        }
    elif wake == "reply":
        slack.humans.append("go ahead")
    state = local.resume_wait(fresh, fresh / example.WORKFLOW, first["run_id"], options=options)
    while state["status"] == "awaiting_confirmation":
        state = local.continue_run(
            fresh, fresh / example.WORKFLOW, state["run_id"], approve=True, options=options
        )
    assert state["status"] == "completed"
    assert state["run_id"] == first["run_id"]
    assert len([s for s in agent.steps if s["step"] == "draft"]) == 1
    assert len(slack.pulls) == (0 if wake == "timeout" else 2)
    if wake in {"polled_reply", "polled_before_expiry"}:
        assert "output" not in options.resume_response


def test_checkpoint_rejects_private_and_traversal_paths(tmp_path):
    for name in [".outcomeci/outcomes/run/../escape", ".outcomeci/outcomes/run/.env", "/tmp/file"]:
        with pytest.raises(ContractError):
            checkpoint.restore(
                tmp_path, "run", [{"path": name, "content_base64": "", "sha256": ""}]
            )


def test_checkpoint_preserves_journal_but_not_broker_credentials(tmp_path):
    run = tmp_path / ".outcomeci/outcomes/run"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "run", "status": "waiting"}))
    broker = tmp_path / ".outcomeci/.broker/run"
    broker.mkdir(parents=True)
    (broker / "journal.json").write_text('{"calls":{}}')
    (broker / "token").write_text("secret-never-copy")
    snapshot = checkpoint.capture(tmp_path, "run")
    assert len(snapshot) == 3
    target = tmp_path / "restored"
    checkpoint.restore(target, "run", snapshot)
    assert (target / ".outcomeci/.broker/run/journal.json").exists()
    assert not (target / ".outcomeci/.broker/run/token").exists()


def test_checkpoint_refuses_symlinks_and_oversize(tmp_path, monkeypatch):
    run = tmp_path / ".outcomeci/outcomes/run"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "run", "status": "waiting"}))
    (run / "bad").symlink_to("/etc/passwd")
    with pytest.raises(ContractError, match="symbolic"):
        checkpoint.capture(tmp_path, "run")
    (run / "bad").unlink()
    monkeypatch.setattr(checkpoint, "MAX_FILE_BYTES", 1)
    with pytest.raises(ContractError, match="large"):
        checkpoint.capture(tmp_path, "run")


def test_checkpoint_rebases_attachment_paths_without_changing_plan(tmp_path):
    original = tmp_path / "original"
    run = original / ".outcomeci/outcomes/run"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "run", "status": "waiting"}))
    attachment = run / "attachments/a.png"
    attachment.parent.mkdir()
    attachment.write_bytes(b"image")
    consultation = {
        "plan": {"path": str(attachment)},
        "turns": [{"files": [{"path": str(attachment)}]}],
    }
    (run / "discuss").mkdir()
    (run / "discuss/consultation.json").write_text(json.dumps(consultation))
    fresh = tmp_path / "fresh"
    checkpoint.restore(fresh, "run", checkpoint.capture(original, "run"))
    restored = json.loads((fresh / ".outcomeci/outcomes/run/discuss/consultation.json").read_text())
    assert restored["plan"] == consultation["plan"]
    assert restored["turns"][0]["files"][0]["path"] == str(
        fresh / ".outcomeci/outcomes/run/attachments/a.png"
    )


def test_cloud_runner_pauses_with_rotation_and_resumes_with_fresh_claim(tmp_path, monkeypatch):
    import importlib

    main = importlib.import_module("outcomeci.cloud_runner.main")
    from outcomeci.cloud_runner.models import Launch

    monkeypatch.setenv("AGENT_PRIVATE_ROOT", str(tmp_path))
    claim = {
        "lease_token": "fresh-lease",
        "content": "workflow",
        "files": {},
        "trigger_name": "webhook",
        "input": {},
        "agent": {
            "provider": "codex",
            "credential": {"token": "fresh-token"},
            "credential_version": 3,
        },
        "vault": {"expires_at": "2099-01-01T00:00:00+00:00", "values": {"slack": "fresh-secret"}},
    }

    class Client:
        paused = None
        completed = []

        def claim_workflow(self):
            return claim

        def workflow_start(self, lease):
            assert lease == "fresh-lease"

        def workflow_pause(self, lease, **payload):
            self.paused = payload

        def workflow_complete(self, lease, status, **payload):
            self.completed.append(status)

    client = Client()
    monkeypatch.setattr(
        "outcomeci.workflow.compiler.compile_workflow",
        lambda _: {
            "workflow": {"spec": {"agents": {"default": {}}}},
            "instructions": {"steps": {"discuss": {}}},
        },
    )

    def trigger(root, config, name, payload, **kwargs):
        kwargs["on_created"]("run")
        path = root / ".outcomeci/outcomes/run"
        path.mkdir(parents=True)
        (path / "run.json").write_text(json.dumps({"run_id": "run", "status": "waiting"}))
        (root / ".codex/auth.json").write_text('{"token":"rotated-token"}')
        raise v1_runtime.DurableWait({"kind": "slack_conversation"})

    monkeypatch.setattr(local, "trigger", trigger)
    launch = Launch("workflow", "invocation", "bootstrap", "https://api.example.com")
    assert main.execute_workflow(launch, client) == 0
    assert client.paused["agent_credential"] == {"token": "rotated-token"}
    assert not client.completed
    claim["resume"] = {
        "run_id": "run",
        "artifacts": client.paused["artifacts"],
        "response": {"reason": "reply"},
    }

    def resumed(root, config, run_id, *, options):
        assert json.loads((root / ".codex/auth.json").read_text()) == {"token": "fresh-token"}
        assert options.credential_resolver("vault:slack") == "fresh-secret"
        assert options.resume_response == {"reason": "reply"}
        assert (root / ".outcomeci/outcomes/run/run.json").exists()
        return {"run_id": "run", "status": "completed", "completed_steps": ["discuss"]}

    monkeypatch.setattr(local, "trigger", lambda *a, **kw: pytest.fail("trigger replayed"))
    monkeypatch.setattr(local, "resume_wait", resumed)
    assert main.execute_workflow(launch, client) == 0
    assert client.completed == ["completed"]


@pytest.mark.parametrize("approved", [True, False])
def test_reaction_wait_deadline_survives_restart_and_accepts_captured_signal(
    tmp_path, monkeypatch, approved
):
    from outcomeci.workflow.compiler import compile_workflow

    compiled = compile_workflow(example.EXAMPLES / "sentry-to-github-pr.outcome.yaml")
    step = next(
        name
        for name in compiled["instructions"]["steps"]
        if v1_runtime.block(compiled, name)["kind"] == "await"
    )
    state = {"run_id": "run", "step": step}
    monkeypatch.setattr(v1_runtime, "value", lambda *a: {"channel": "C1", "ts": "100.0"})
    monkeypatch.setattr(v1_runtime, "_covers", lambda *a: [])
    monkeypatch.setattr(v1_runtime, "_poll", lambda *a: {"output": {"reactions": []}})
    finished = []
    monkeypatch.setattr(
        local, "_finish_interaction", lambda *a, **kw: finished.append(kw["status"])
    )
    with pytest.raises(v1_runtime.DurableWait):
        v1_runtime.run_await(tmp_path, compiled, state, step, lambda _: "secret", durable=True)
    wait = tmp_path / ".outcomeci/outcomes/run" / step / "wait.json"
    wait.write_text('{"expires_at":1}')
    response = (
        {"reason": "reply", "output": {"reactions": [{"name": "+1", "count": 1, "users": ["U1"]}]}}
        if approved
        else None
    )
    monkeypatch.setattr(
        v1_runtime, "_poll", lambda *a: pytest.fail("expired wait must not poll again")
    )
    assert (
        v1_runtime.run_await(
            tmp_path, compiled, state, step, lambda _: "secret", durable=True, response=response
        )
        is approved
    )
    assert finished == ["approved" if approved else "expired"]


def test_cloud_checkpoint_resumes_under_the_claimed_revision_id(tmp_path, monkeypatch):
    """A cloud checkpoint is identified by the revision id the claim named, so a
    runner whose compiled hash differs (another connector or compiler build of
    the same revision) still resumes it; another revision id does not."""
    root = _slack_root(tmp_path, monkeypatch)
    config = root / example.WORKFLOW
    slack, agent = example.Slack([]), example.Agent()
    real = httpx.Client
    monkeypatch.setattr(
        integrations.httpx,
        "Client",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(slack)}),
    )
    monkeypatch.setattr(local, "invoke", agent)
    options = local.ExecutionOptions(
        durable_waits=True,
        workflow_revision_id="rev-1",
        credential_resolver=lambda ref: "xoxb-test",
        policy_reviewer=lambda p: {
            "decision": "allow",
            "proposal_sha256": p["proposal_sha256"],
            "reason": "ok",
        },
    )
    first = local.trigger(root, config, "webhook", example._payload(), options=options)
    with pytest.raises(v1_runtime.DurableWait):
        local.continue_run(root, config, first["run_id"], approve=True, options=options)
    state = json.loads((root / ".outcomeci/outcomes" / first["run_id"] / "run.json").read_text())
    assert state["resume_workflow_revision_id"] == "rev-1"
    assert state["resume_workflow_revision"]

    snapshot = checkpoint.capture(root, first["run_id"])
    fresh = tmp_path / "fresh"
    shutil.copytree(example.EXAMPLES, fresh)
    rebuilt = fresh / example.WORKFLOW
    # The same revision compiled by a different build: its hash no longer matches.
    rebuilt.write_text(
        config.read_text().replace("Find every repository", "Carefully find every repository")
    )
    checkpoint.restore(fresh, first["run_id"], snapshot)
    with pytest.raises(v1_runtime.DurableWait):
        local.resume_wait(fresh, rebuilt, first["run_id"], options=options)

    other = local.ExecutionOptions(durable_waits=True, workflow_revision_id="rev-2")
    with pytest.raises(local.ExecutionError, match="workflow changed"):
        local.resume_wait(fresh, rebuilt, first["run_id"], options=other)
