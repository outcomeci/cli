"""Bounded Standup phase execution."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from jsonschema import ValidationError
from jsonschema import validate as validate_json

from .config import compile_workflow
from .manifest import build_manifest
from .process import ExecutionError, GitHub, invoke
from .security import private_path
from .transcripts import _transcripts


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(argv: list[str], cwd: Path, env: dict[str, str]) -> str:
    import subprocess

    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise ExecutionError(f"git failed: {result.stderr[-1000:]}", True)
    return result.stdout.strip()


def _open_pull_request(
    repository: str, base_branch: str, branch: str, title: str, body: str
) -> tuple[int, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise ExecutionError("GitHub token is unavailable")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body_json = {"title": title, "body": body, "head": branch, "base": base_branch}
    try:
        with httpx.stream(
            "POST",
            f"https://api.github.com/repos/{repository}/pulls",
            json=body_json,
            headers=headers,
            timeout=30,
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                raise ExecutionError(
                    f"GitHub could not create a pull request for {repository}", True
                )
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 1_048_576:
                    break
            payload = json.loads(bytes(raw))
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise ExecutionError(
            f"GitHub could not create a pull request for {repository}", True
        ) from exc
    number = payload.get("number") if isinstance(payload, dict) else None
    url = payload.get("html_url") if isinstance(payload, dict) else None
    if (
        not isinstance(number, int)
        or number < 1
        or not isinstance(url, str)
        or not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/pull/\d+", url)
    ):
        raise ExecutionError(f"GitHub returned an invalid pull request for {repository}")
    return number, url


def _claim(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError("invalid outcome claim") from exc
    required = {
        "outcome_run_id",
        "workflow_run_id",
        "phase",
        "trajectory_version",
        "agent",
        "model",
        "artifact_backend",
        "targets",
        "intent_context",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or not isinstance(value["phase"], str)
        or value["agent"] not in {"codex", "claude", "opencode"}
    ):
        raise ExecutionError("invalid outcome claim")
    if not isinstance(value["targets"], list) or (
        value["phase"] != "intake" and not value["targets"]
    ):
        raise ExecutionError("invalid outcome targets")
    backend = value["artifact_backend"]
    if not isinstance(backend, dict) or backend.get("provider") not in {"outcomeci", "github"}:
        raise ExecutionError("invalid artifact backend")
    if backend["provider"] == "github" and not isinstance(backend.get("repository"), str):
        raise ExecutionError("GitHub artifact backend requires a repository")
    if backend["provider"] == "outcomeci" and not isinstance(backend.get("files"), dict):
        raise ExecutionError("OutcomeCI artifact backend requires files")
    return value


def _managed_state(state: Path, backend: dict[str, Any]) -> None:
    files = backend["files"]
    total = 0
    for relative, encoded in files.items():
        if not isinstance(relative, str) or not isinstance(encoded, str):
            raise ExecutionError("invalid managed artifact")
        target = (state / relative).resolve()
        try:
            target.relative_to(state.resolve())
        except ValueError as exc:
            raise ExecutionError("managed artifact escapes the state directory") from exc
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ExecutionError("invalid managed artifact encoding") from exc
        total += len(content)
        if len(content) > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
            raise ExecutionError("managed artifact bundle is too large")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _managed_artifacts(state: Path, outcome_root: Path) -> list[dict[str, Any]]:
    result, total = [], 0
    for path in sorted(
        item
        for item in outcome_root.rglob("*")
        if item.is_file() and not private_path(item.relative_to(outcome_root))
    ):
        content = path.read_bytes()
        total += len(content)
        if len(content) > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
            raise ExecutionError("managed outcome artifacts exceed the result limit")
        result.append(
            {
                "path": str(path.relative_to(state)),
                "content_base64": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return result


def _contract_path(root: Path, contract: dict[str, Any]) -> Path:
    path = (root / contract["path"]).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ExecutionError(f"artifact {contract['name']} escapes the outcome directory") from exc
    return path


def _validate_contract_outputs(compiled: dict[str, Any], root: Path, phase: str) -> list[Path]:
    paths: list[Path] = []
    for contract in compiled["instructions"]["phases"][phase]["expects"]["outputs"]:
        path = _contract_path(root, contract)
        if not path.exists():
            if contract["required"]:
                raise ExecutionError(
                    f"missing required output {phase}.{contract['name']}: {contract['path']}"
                )
            continue
        paths.append(path)
        if contract["media_type"] == "inode/directory":
            if not path.is_dir() or not any(item.is_file() for item in path.rglob("*")):
                raise ExecutionError(
                    f"output {phase}.{contract['name']} must be a non-empty directory"
                )
            continue
        if not path.is_file() or not path.read_bytes():
            raise ExecutionError(f"output {phase}.{contract['name']} must be a non-empty file")
        if contract["media_type"] == "application/json" or contract.get("schema"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if contract.get("schema"):
                    validate_json(
                        value, compiled["instructions"]["schemas"][contract["schema"]]["value"]
                    )
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise ExecutionError(
                    f"output {phase}.{contract['name']} failed JSON validation: {exc}"
                ) from exc
    return paths


def _validate_claim_phase(compiled: dict[str, Any], outcome_root: Path, phase: str) -> None:
    phases = compiled["instructions"]["phases"]
    if phase not in phases:
        raise ExecutionError(f"workflow has no phase {phase}")
    for dependency in phases[phase]["needs"]:
        for contract in phases[dependency]["expects"]["outputs"]:
            if contract["required"] and not _contract_path(outcome_root, contract).exists():
                raise ExecutionError(
                    f"phase {phase} is blocked by incomplete dependency {dependency}"
                )


def _expected(root: Path, repositories: list[str], phase: str) -> list[Path]:
    paths = [root / "standup.md"]
    if phase == "intake":
        return paths + [root / "intake" / "trajectory.json"]
    for repository in repositories:
        slug = _slug(repository)
        if phase == "plan":
            paths += [root / "specs" / slug / "spec.md", root / "plans" / slug / "plan.md"]
        elif phase == "tasks":
            paths += [root / "tasks" / "repositories" / f"{slug}.md"]
    if phase == "tasks":
        paths += [root / "tasks" / "tasks.md"]
    return paths


def _publish_implementation(
    github: GitHub,
    claim: dict[str, Any],
    repositories: list[str],
    products: list[Path],
    base_commits: dict[str, str],
    base_branches: dict[str, str],
) -> list[dict[str, Any]]:
    publications: list[dict[str, Any]] = []
    branch = f"oci/{_slug(claim['outcome_run_id'])[:40]}-{claim['trajectory_version']}"
    for repository, checkout in zip(repositories, products, strict=True):
        base_branch = base_branches[repository]
        dirty = github.run(["git", "status", "--porcelain=v1"], checkout)
        head = github.run(["git", "rev-parse", "HEAD"], checkout)
        if not dirty and head == base_commits[repository]:
            publications.append(
                {"repository": repository, "status": "no_change", "base_commit_sha": head}
            )
            continue
        if dirty:
            github.run(["git", "config", "user.name", "OutcomeCI"], checkout)
            github.run(["git", "config", "user.email", "runner@outcomeci.com"], checkout)
            github.run(["git", "add", "--all"], checkout)
            github.run(
                ["git", "commit", "-m", f"feat: implement outcome {claim['outcome_run_id']}"],
                checkout,
            )
        head = github.run(["git", "rev-parse", "HEAD"], checkout)
        github.run(
            ["git", "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"],
            checkout,
            600,
        )
        title = str(
            claim.get("intent_context", {}).get("title")
            or f"Implement outcome {claim['outcome_run_id']}"
        )[:240]
        body = f"OutcomeCI run `{claim['outcome_run_id']}`\n\nGenerated from the approved workflow trajectory."
        number, url = _open_pull_request(repository, base_branch, branch, title, body)
        publications.append(
            {
                "repository": repository,
                "status": "pr_opened",
                "base_commit_sha": base_commits[repository],
                "head_commit_sha": head,
                "branch": branch,
                "pull_request_number": number,
                "pull_request_url": url,
            }
        )
    return publications


def _validate_trajectory(value: Any, claim: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != "1":
        raise ExecutionError("agent returned an invalid intake trajectory")
    if value.get("ontology_revision_id") != claim["intent_context"].get("ontology_revision_id"):
        raise ExecutionError("agent changed the pinned Digital Twin revision")
    targets = value.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ExecutionError("agent returned no intake repository targets")
    roles = {
        "entry_point",
        "orchestration",
        "domain_logic",
        "persistence",
        "provider_adapter",
        "schema_contract",
        "validation",
    }
    dispositions = {"modify", "add", "inspect", "validate", "coordinate"}
    for target in targets:
        if not isinstance(target, dict) or not all(
            isinstance(target.get(key), str) and target[key]
            for key in ("repository_id", "repository", "rationale")
        ):
            raise ExecutionError("agent returned an invalid intake repository target")
        for candidate in target.get("candidates", []):
            if (
                not isinstance(candidate, dict)
                or candidate.get("role") not in roles
                or candidate.get("disposition") not in dispositions
            ):
                raise ExecutionError(
                    "agent returned an intake candidate outside the stable contract"
                )
            responsibility = candidate.get("responsibility")
            if responsibility is not None and (
                not isinstance(responsibility, str)
                or not responsibility.strip()
                or len(responsibility) > 100
            ):
                raise ExecutionError("agent returned an invalid intake responsibility")
    return value


def run(claim_path: Path, workspace: Path) -> dict[str, Any]:
    claim = _claim(claim_path)
    needs_github = bool(claim["targets"]) or claim["artifact_backend"]["provider"] == "github"
    github = GitHub(os.environ.get("GITHUB_TOKEN", "")) if needs_github else None
    workspace.mkdir(parents=True, exist_ok=True)
    state = workspace / "state"
    backend = claim["artifact_backend"]
    state_repository = backend.get("repository") if backend["provider"] == "github" else None
    state_base = None
    if backend["provider"] == "github":
        assert github is not None
        github.clone(state_repository, state)
        state_base = _git(["git", "rev-parse", "HEAD"], state, github.env)
    else:
        state.mkdir(parents=True, exist_ok=True)
        _managed_state(state, backend)
    constitution = state / ".outcomeci" / "constitution.md"
    workflow = state / "outcome.yml"
    if not constitution.is_file() or not workflow.is_file():
        raise ExecutionError("state repository is not initialized for OutcomeCI")
    repositories, products, base_commits, base_branches = [], [], {}, {}
    for item in claim["targets"]:
        assert github is not None
        repository = item.get("repository")
        if not isinstance(repository, str):
            raise ExecutionError("invalid outcome target")
        checkout = workspace / "repositories" / _slug(repository)
        github.clone(repository, checkout)
        repositories.append(repository)
        products.append(checkout)
        base_commits[repository] = _git(["git", "rev-parse", "HEAD"], checkout, github.env)
        base_branches[repository] = str(item.get("base_branch") or "main")
    outcome_root = state / ".outcomeci" / "outcomes" / claim["outcome_run_id"]
    outcome_root.mkdir(parents=True, exist_ok=True)
    compiled = compile_workflow(workflow)
    _validate_claim_phase(compiled, outcome_root, claim["phase"])
    configured_policy = compiled["instructions"]["phases"][claim["phase"]]["policy"]
    runner = configured_policy.get("runner") or claim["agent"]
    model = (
        configured_policy.get("model")
        if configured_policy.get("model") is not None
        else claim.get("model")
    )
    shared = compiled["instructions"]["orchestrator"]["content"]
    phase_instructions = compiled["instructions"]["phases"][claim["phase"]]["content"]
    payload = {
        "claim": claim,
        "artifact_workspace": str(state),
        "product_repositories": [
            {"name_with_owner": name, "base_commit_sha": base_commits[name], "checkout": str(path)}
            for name, path in zip(repositories, products, strict=True)
        ],
    }
    implementation = claim["phase"] == "implementation"
    boundary = (
        "Product repositories are writable for approved implementation. Modify source and tests, but do not commit, push, or open pull requests; the runner owns publication."
        if implementation
        else "Product repositories are read-only. Do not commit, push, or open pull requests in product repositories."
    )
    prompt = f"{shared}\n\n{phase_instructions}\n\nWrite all durable artifacts beneath {outcome_root}. {boundary} The `oci twin search` command is the only live Digital Twin interface.\n\n{json.dumps(payload, separators=(',', ':'))}"
    summary = invoke(runner, model, prompt, workspace, 7200)
    publications: list[dict[str, Any]] | None = None
    if implementation:
        assert github is not None
        publications = _publish_implementation(
            github, claim, repositories, products, base_commits, base_branches
        )
        publication_path = outcome_root / "implementation" / "publication.json"
        publication_path.parent.mkdir(parents=True, exist_ok=True)
        publication_path.write_text(
            json.dumps(
                {"schema_version": 1, "publications": publications}, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        for repository, checkout in zip(repositories, products, strict=True):
            if _git(["git", "status", "--porcelain=v1"], checkout, github.env):
                raise ExecutionError(f"planning modified product repository {repository}")
    _validate_contract_outputs(compiled, outcome_root, claim["phase"])
    standup = (outcome_root / "standup.md").read_text(encoding="utf-8")
    if "# Standup:" not in standup or "**Status**: active" not in standup:
        raise ExecutionError("agent did not produce a valid active Standup")
    intake_context = None
    if claim["phase"] == "intake":
        intake_context = _validate_trajectory(
            json.loads((outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8")),
            claim,
        )
    transcripts = _transcripts(runner, outcome_root, claim["phase"])
    manifest = build_manifest(
        outcome_root=outcome_root,
        artifact_base=state,
        run_id=claim["outcome_run_id"],
        workflow_run_id=claim["workflow_run_id"],
        trajectory_version=claim["trajectory_version"],
        phase=claim["phase"],
        workflow_revision=compiled["workflow_revision"],
        backend_provider=backend["provider"],
        state_repository=state_repository,
        context_provider=compiled["workflow"]["spec"]["context"].get("provider", "outcomeci"),
        context_revision_id=claim["intent_context"].get("ontology_revision_id"),
        constitution_sha256=_sha(constitution),
        repository_base_commits=base_commits,
        runner=runner,
        model=model,
        transcript=transcripts,
        phase_contract=compiled["instructions"]["phases"][claim["phase"]]["expects"],
    )
    artifacts = manifest["artifacts"]
    (outcome_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    commit = None
    managed_artifacts = None
    if backend["provider"] == "github":
        assert github is not None
        _git(["git", "add", "--", str(outcome_root.relative_to(state))], state, github.env)
        _git(["git", "config", "user.name", "OutcomeCI"], state, github.env)
        _git(["git", "config", "user.email", "runner@outcomeci.com"], state, github.env)
        _git(
            [
                "git",
                "commit",
                "-m",
                f"docs: record outcome {claim['outcome_run_id']} {claim['phase']}",
            ],
            state,
            github.env,
        )
        commit = _git(["git", "rev-parse", "HEAD"], state, github.env)
        _git(
            [
                "git",
                "push",
                f"--force-with-lease=refs/heads/main:{state_base}",
                "origin",
                "HEAD:refs/heads/main",
            ],
            state,
            github.env,
        )
    else:
        managed_artifacts = _managed_artifacts(state, outcome_root)
    status = (
        "completed"
        if implementation
        else (
            "awaiting_confirmation"
            if claim["phase"] in {"intake", "plan"}
            else "ready_for_implementation"
        )
    )
    result = {
        "status": status,
        "phase": claim["phase"],
        "state_commit_sha": commit,
        "constitution_sha": _sha(constitution),
        "manifest": manifest,
        "artifact_paths": artifacts,
        "summary": summary[-1000:],
    }
    if publications is not None:
        result["publications"] = publications
    if managed_artifacts is not None:
        result["managed_artifacts"] = managed_artifacts
    if claim["phase"] == "intake":
        result["intent_context"] = intake_context
    return result
