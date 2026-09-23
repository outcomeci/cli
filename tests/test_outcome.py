from pathlib import Path

import httpx
import pytest

from outcomeci import outcome
from outcomeci import outcome as outcome_module
from outcomeci.outcome import (
    _claim,
    _expected,
    _managed_artifacts,
    _managed_state,
    _open_pull_request,
    _publish_implementation,
    _transcripts,
    _validate_claim_phase,
    _validate_trajectory,
)
from outcomeci.process import ExecutionError


def test_claim_accepts_managed_plan(tmp_path: Path) -> None:
    path = tmp_path / "claim.json"
    path.write_text(
        '{"outcome_run_id":"r","workflow_run_id":"w","phase":"plan","trajectory_version":1,"agent":"codex","model":null,"artifact_backend":{"provider":"outcomeci","files":{}},"targets":[{"repository":"org/repo"}],"intent_context":{}}'
    )
    assert _claim(path)["phase"] == "plan"


def test_claim_accepts_custom_phase_for_workflow_validation(tmp_path: Path) -> None:
    path = tmp_path / "claim.json"
    path.write_text(
        '{"outcome_run_id":"r","workflow_run_id":"w","phase":"deploy","trajectory_version":1,"agent":"codex","model":null,"artifact_backend":{"provider":"github","repository":"org/state"},"targets":[{"repository":"org/repo"}],"intent_context":{}}'
    )
    assert _claim(path)["phase"] == "deploy"


def test_managed_backend_round_trips_bounded_artifacts(tmp_path: Path) -> None:
    import base64

    state = tmp_path / "state"
    state.mkdir()
    _managed_state(
        state, {"files": {"outcome.yml": base64.b64encode(b"kind: OutcomeWorkflow\n").decode()}}
    )
    assert (state / "outcome.yml").read_text() == "kind: OutcomeWorkflow\n"
    artifact = state / ".outcomeci" / "outcomes" / "run_1" / "standup.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Standup\n")
    result = _managed_artifacts(state, artifact.parent)
    assert result[0]["path"] == ".outcomeci/outcomes/run_1/standup.md"
    assert base64.b64decode(result[0]["content_base64"]) == b"# Standup\n"


def test_plan_requires_standup_and_repo_artifacts(tmp_path: Path) -> None:
    values = _expected(tmp_path, ["outcomeci/www"], "plan")
    assert tmp_path / "standup.md" in values
    assert tmp_path / "specs/outcomeci-www/spec.md" in values
    assert tmp_path / "plans/outcomeci-www/plan.md" in values


def test_responsibility_extends_stable_candidate_role() -> None:
    claim = {"intent_context": {"ontology_revision_id": "rev-1"}}
    value = {
        "schema_version": "1",
        "ontology_revision_id": "rev-1",
        "targets": [
            {
                "repository_id": "R1",
                "repository": "outcomeci/api",
                "rationale": "match",
                "candidates": [
                    {
                        "role": "domain_logic",
                        "responsibility": "usage_source",
                        "disposition": "inspect",
                    }
                ],
            }
        ],
    }
    assert (
        _validate_trajectory(value, claim)["targets"][0]["candidates"][0]["responsibility"]
        == "usage_source"
    )


def test_arbitrary_value_cannot_replace_stable_role() -> None:
    claim = {"intent_context": {"ontology_revision_id": "rev-1"}}
    value = {
        "schema_version": "1",
        "ontology_revision_id": "rev-1",
        "targets": [
            {
                "repository_id": "R1",
                "repository": "outcomeci/api",
                "rationale": "match",
                "candidates": [{"role": "usage_source", "disposition": "inspect"}],
            }
        ],
    }
    with pytest.raises(ExecutionError, match="stable contract"):
        _validate_trajectory(value, claim)


def test_interactive_transcript_captures_only_bytes_after_begin(
    tmp_path: Path, monkeypatch
) -> None:
    session = tmp_path / "rollout-session-1.jsonl"
    prefix = '{"type":"session_meta","payload":{"id":"session-1","cwd":"/repo"}}\n'
    session.write_text(prefix)
    offset = session.stat().st_size
    session.write_text(prefix + '{"usage":{"input_tokens":12,"output_tokens":4}}\n')
    monkeypatch.setattr(outcome, "_sessions", lambda agent: [session])

    captured = _transcripts(
        "codex",
        tmp_path / "outcome",
        "intake",
        session_id="session-1",
        byte_offset=offset,
        workspace=Path("/repo"),
    )

    assert captured["files"][0]["source_offset"] == offset
    copied = tmp_path / "outcome" / captured["files"][0]["path"]
    assert "session_meta" not in copied.read_text()
    assert captured["usage_records"] == 1
    assert captured["usage"][0]["input_tokens"] == 12


def test_managed_claim_phase_must_have_completed_dependencies(tmp_path: Path) -> None:
    compiled = {
        "instructions": {
            "phases": {
                "intake": {
                    "needs": [],
                    "expects": {
                        "outputs": [
                            {"name": "packet", "path": "intake/packet.json", "required": True}
                        ]
                    },
                },
                "plan": {"needs": ["intake"], "expects": {"outputs": []}},
            }
        }
    }
    with pytest.raises(ExecutionError, match="blocked by incomplete dependency intake"):
        _validate_claim_phase(compiled, tmp_path, "plan")
    packet = tmp_path / "intake" / "packet.json"
    packet.parent.mkdir()
    packet.write_text("{}")
    _validate_claim_phase(compiled, tmp_path, "plan")


def test_implementation_publication_is_runner_owned(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()

    class GitHub:
        def run(self, argv, cwd, timeout=300):
            if argv[:3] == ["git", "branch", "--show-current"]:
                return "main"
            if argv[:3] == ["git", "status", "--porcelain=v1"]:
                return " M app.py"
            if argv[:3] == ["git", "rev-parse", "HEAD"]:
                return "b" * 40
            return ""

    monkeypatch.setattr(
        outcome_module,
        "_open_pull_request",
        lambda *args: (12, "https://github.com/outcomeci/repo/pull/12"),
    )

    result = _publish_implementation(
        GitHub(),
        {
            "outcome_run_id": "run_1",
            "trajectory_version": 2,
            "intent_context": {"title": "Ship it"},
        },
        ["outcomeci/repo"],
        [checkout],
        {"outcomeci/repo": "a" * 40},
        {"outcomeci/repo": "main"},
    )

    assert result == [
        {
            "repository": "outcomeci/repo",
            "status": "pr_opened",
            "base_commit_sha": "a" * 40,
            "head_commit_sha": "b" * 40,
            "branch": "oci/run_1-2",
            "pull_request_number": 12,
            "pull_request_url": "https://github.com/outcomeci/repo/pull/12",
        }
    ]


def _fake_stream(handler):
    def stream(method, url, *, json=None, headers=None, timeout=None, follow_redirects=None):
        return httpx.Client(transport=httpx.MockTransport(handler)).stream(
            method, url, json=json, headers=headers
        )

    return stream


def test_open_pull_request_sends_the_bearer_token_and_returns_the_pr(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(
            201, json={"number": 42, "html_url": "https://github.com/outcomeci/repo/pull/42"}
        )

    monkeypatch.setattr(outcome.httpx, "stream", _fake_stream(handler))

    number, url = _open_pull_request("outcomeci/repo", "main", "feature", "Ship it", "Body")

    assert (number, url) == (42, "https://github.com/outcomeci/repo/pull/42")
    assert captured["authorization"] == "Bearer gh-token"
    assert captured["url"] == "https://api.github.com/repos/outcomeci/repo/pulls"


def test_open_pull_request_raises_on_an_http_error_status(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    handler = lambda request: httpx.Response(422, json={"message": "Validation failed"})  # noqa: E731
    monkeypatch.setattr(outcome.httpx, "stream", _fake_stream(handler))

    with pytest.raises(ExecutionError, match="could not create a pull request"):
        _open_pull_request("outcomeci/repo", "main", "feature", "Ship it", "Body")


def test_open_pull_request_rejects_a_malformed_success_payload(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    handler = lambda request: httpx.Response(201, json={"unexpected": True})  # noqa: E731
    monkeypatch.setattr(outcome.httpx, "stream", _fake_stream(handler))

    with pytest.raises(ExecutionError, match="invalid pull request"):
        _open_pull_request("outcomeci/repo", "main", "feature", "Ship it", "Body")
