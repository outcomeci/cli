"""Bounded Standup phase execution."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate as validate_json

from .process import ExecutionError, GitHub, invoke
from .config import compile_workflow
from .manifest import build_manifest


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


def _claim(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError("invalid outcome claim") from exc
    required = {"outcome_run_id", "workflow_run_id", "phase", "trajectory_version", "agent", "model", "state_repository", "targets", "intent_context"}
    if not isinstance(value, dict) or not required <= set(value) or not isinstance(value["phase"], str) or value["agent"] not in {"codex", "claude"}:
        raise ExecutionError("invalid outcome claim")
    if not isinstance(value["targets"], list) or (value["phase"] != "intake" and not value["targets"]):
        raise ExecutionError("invalid outcome targets")
    return value


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
                raise ExecutionError(f"missing required output {phase}.{contract['name']}: {contract['path']}")
            continue
        paths.append(path)
        if contract["media_type"] == "inode/directory":
            if not path.is_dir() or not any(item.is_file() for item in path.rglob("*")):
                raise ExecutionError(f"output {phase}.{contract['name']} must be a non-empty directory")
            continue
        if not path.is_file() or not path.read_bytes():
            raise ExecutionError(f"output {phase}.{contract['name']} must be a non-empty file")
        if contract["media_type"] == "application/json" or contract.get("schema"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if contract.get("schema"):
                    validate_json(value, compiled["instructions"]["schemas"][contract["schema"]]["value"])
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise ExecutionError(f"output {phase}.{contract['name']} failed JSON validation: {exc}") from exc
    return paths


def _validate_claim_phase(compiled: dict[str, Any], outcome_root: Path, phase: str) -> None:
    phases = compiled["instructions"]["phases"]
    if phase not in phases:
        raise ExecutionError(f"workflow has no phase {phase}")
    for dependency in phases[phase]["needs"]:
        for contract in phases[dependency]["expects"]["outputs"]:
            if contract["required"] and not _contract_path(outcome_root, contract).exists():
                raise ExecutionError(f"phase {phase} is blocked by incomplete dependency {dependency}")


def _sessions(agent: str) -> list[Path]:
    if agent == "codex":
        configured = os.environ.get("CODEX_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
        found = root.glob("sessions/**/*.jsonl") if root.is_dir() else []
    else:
        root = Path(os.environ.get("HOME", "")) / ".claude" / "projects"
        found = root.glob("**/*.jsonl") if root.is_dir() else []
    return sorted(path for path in found if path.is_file() and path.stat().st_size <= 50 * 1024 * 1024)


def _usage(path: Path, provider: str) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                usage = item.get("usage")
                info = item.get("info")
                if item.get("type") == "token_count" and isinstance(info, dict):
                    usage = info.get("last_token_usage")
                if isinstance(usage, dict) and any(key in usage for key in ("input_tokens", "output_tokens", "inputTokens", "outputTokens")):
                    records.append({"provider": provider, "source_line": line_number, "occurred_at": item.get("timestamp") or value.get("timestamp"), "input_tokens": int(usage.get("input_tokens") or usage.get("inputTokens") or 0), "output_tokens": int(usage.get("output_tokens") or usage.get("outputTokens") or 0), "cache_read_tokens": int(usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens") or 0), "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_input_tokens") or 0)})
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return list({json.dumps(item, sort_keys=True): item for item in records}.values())


def _session_details(path: Path, agent: str) -> tuple[str | None, str | None]:
    """Return the session id and cwd without retaining conversation content."""
    found_id: str | None = None
    found_cwd: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for _, line in zip(range(100), source):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
                session_id = value.get("sessionId") or payload.get("id")
                cwd = value.get("cwd") or payload.get("cwd")
                if session_id and not found_id:
                    found_id = str(session_id)
                if cwd and not found_cwd:
                    found_cwd = str(cwd)
                if found_id and found_cwd:
                    break
    except OSError:
        pass
    return found_id, found_cwd


def _select_sessions(agent: str, session_id: str | None, workspace: Path | None, since: str | None) -> list[Path]:
    candidates = _sessions(agent)
    if session_id:
        exact = [path for path in candidates if session_id in path.name or _session_details(path, agent)[0] == session_id]
        if exact:
            return exact[:1]
    expected = str(workspace.resolve()) if workspace else None
    scoped = [path for path in candidates if expected and _session_details(path, agent)[1] == expected]
    if since:
        try:
            threshold = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
            scoped = [path for path in scoped if path.stat().st_mtime >= threshold]
        except ValueError:
            pass
    return sorted(scoped, key=lambda path: path.stat().st_mtime, reverse=True)[:1]


def _transcripts(
    agent: str,
    root: Path,
    phase: str,
    *,
    session_id: str | None = None,
    byte_offset: int = 0,
    workspace: Path | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    target = root / "transcripts" / phase / agent
    target.mkdir(parents=True, exist_ok=True)
    files, usage, total = [], [], 0
    sources = _select_sessions(agent, session_id, workspace, since) if (session_id or workspace) else _sessions(agent)
    for index, source in enumerate(sources, 1):
        source_size = source.stat().st_size
        offset = min(byte_offset, source_size) if index == 1 else 0
        size = source_size - offset
        if total + size > 100 * 1024 * 1024:
            break
        destination = target / f"{index:02d}-{source.name}"
        with source.open("rb") as input_file, destination.open("wb") as output_file:
            input_file.seek(offset)
            shutil.copyfileobj(input_file, output_file)
        total += size
        relative = str(destination.relative_to(root))
        records = [{**item, "transcript_path": relative} for item in _usage(destination, agent)]
        usage.extend(records)
        files.append({"path": relative, "byte_size": size, "source_offset": offset, "sha256": _sha(destination), "usage_records": len(records)})
    usage_path = root / "transcripts" / phase / "usage.json"
    usage_path.write_text(json.dumps({"schema_version": 1, "provider": agent, "phase": phase, "records": usage}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"provider": agent, "phase": phase, "files": files, "usage_path": str(usage_path.relative_to(root)), "usage_records": len(usage), "usage": usage[:20000]}


def _expected(root: Path, repositories: list[str], phase: str) -> list[Path]:
    paths = [root / "standup.md"]
    if phase == "intake":
        return paths + [root / "intake" / "trajectory.json"]
    for repository in repositories:
        slug = _slug(repository)
        if phase == "plan":
            paths += [root / "specs" / slug / "spec.md", root / "plans" / slug / "plan.md"]
        else:
            paths += [root / "tasks" / "repositories" / f"{slug}.md"]
    if phase == "tasks":
        paths += [root / "tasks" / "tasks.md"]
    return paths


def _validate_trajectory(value: Any, claim: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != "1":
        raise ExecutionError("agent returned an invalid intake trajectory")
    if value.get("ontology_revision_id") != claim["intent_context"].get("ontology_revision_id"):
        raise ExecutionError("agent changed the pinned Digital Twin revision")
    targets = value.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ExecutionError("agent returned no intake repository targets")
    roles = {"entry_point", "orchestration", "domain_logic", "persistence", "provider_adapter", "schema_contract", "validation"}
    dispositions = {"modify", "add", "inspect", "validate", "coordinate"}
    for target in targets:
        if not isinstance(target, dict) or not all(isinstance(target.get(key), str) and target[key] for key in ("repository_id", "repository", "rationale")):
            raise ExecutionError("agent returned an invalid intake repository target")
        for candidate in target.get("candidates", []):
            if not isinstance(candidate, dict) or candidate.get("role") not in roles or candidate.get("disposition") not in dispositions:
                raise ExecutionError("agent returned an intake candidate outside the stable contract")
            responsibility = candidate.get("responsibility")
            if responsibility is not None and (not isinstance(responsibility, str) or not responsibility.strip() or len(responsibility) > 100):
                raise ExecutionError("agent returned an invalid intake responsibility")
    return value


def run(claim_path: Path, workspace: Path) -> dict[str, Any]:
    claim = _claim(claim_path)
    github = GitHub(os.environ.get("GITHUB_TOKEN", ""))
    workspace.mkdir(parents=True, exist_ok=True)
    state = workspace / "state"
    github.clone(claim["state_repository"], state)
    state_base = _git(["git", "rev-parse", "HEAD"], state, github.env)
    constitution = state / ".outcomeci" / "constitution.md"
    workflow = state / "outcome.yml"
    if not constitution.is_file() or not workflow.is_file():
        raise ExecutionError("state repository is not initialized for OutcomeCI")
    repositories, products, base_commits = [], [], {}
    for item in claim["targets"]:
        repository = item.get("repository")
        if not isinstance(repository, str):
            raise ExecutionError("invalid outcome target")
        checkout = workspace / "repositories" / _slug(repository)
        github.clone(repository, checkout)
        repositories.append(repository)
        products.append(checkout)
        base_commits[repository] = _git(["git", "rev-parse", "HEAD"], checkout, github.env)
    outcome_root = state / ".outcomeci" / "outcomes" / claim["outcome_run_id"]
    outcome_root.mkdir(parents=True, exist_ok=True)
    compiled = compile_workflow(workflow)
    _validate_claim_phase(compiled, outcome_root, claim["phase"])
    configured_policy = compiled["instructions"]["phases"][claim["phase"]]["policy"]
    runner = configured_policy.get("runner") or claim["agent"]
    model = configured_policy.get("model") if configured_policy.get("model") is not None else claim.get("model")
    shared = compiled["instructions"]["orchestrator"]["content"]
    phase_instructions = compiled["instructions"]["phases"][claim["phase"]]["content"]
    payload = {"claim": claim, "state_repository": str(state), "product_repositories": [{"name_with_owner": name, "base_commit_sha": base_commits[name], "checkout": str(path)} for name, path in zip(repositories, products)]}
    prompt = f"{shared}\n\n{phase_instructions}\n\nWrite all durable artifacts beneath {outcome_root}. Product repositories are read-only. The `oci twin search` command is the only live Digital Twin interface. Do not commit, push, or open pull requests in product repositories.\n\n{json.dumps(payload, separators=(',', ':'))}"
    summary = invoke(runner, model, prompt, workspace, 7200)
    for repository, checkout in zip(repositories, products):
        if _git(["git", "status", "--porcelain=v1"], checkout, github.env):
            raise ExecutionError(f"planning modified product repository {repository}")
    _validate_contract_outputs(compiled, outcome_root, claim["phase"])
    standup = (outcome_root / "standup.md").read_text(encoding="utf-8")
    if "# Standup:" not in standup or "**Status**: active" not in standup:
        raise ExecutionError("agent did not produce a valid active Standup")
    intake_context = None
    if claim["phase"] == "intake":
        intake_context = _validate_trajectory(json.loads((outcome_root / "intake" / "trajectory.json").read_text(encoding="utf-8")), claim)
    transcripts = _transcripts(runner, outcome_root, claim["phase"])
    manifest = build_manifest(
        outcome_root=outcome_root,
        artifact_base=state,
        run_id=claim["outcome_run_id"],
        workflow_run_id=claim["workflow_run_id"],
        trajectory_version=claim["trajectory_version"],
        phase=claim["phase"],
        workflow_revision=compiled["workflow_revision"],
        backend_provider="outcomeci",
        state_repository=claim["state_repository"],
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
    (outcome_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _git(["git", "add", "--", str(outcome_root.relative_to(state))], state, github.env)
    _git(["git", "config", "user.name", "OutcomeCI"], state, github.env)
    _git(["git", "config", "user.email", "runner@outcomeci.com"], state, github.env)
    _git(["git", "commit", "-m", f"docs: record outcome {claim['outcome_run_id']} {claim['phase']}"], state, github.env)
    commit = _git(["git", "rev-parse", "HEAD"], state, github.env)
    _git(["git", "push", f"--force-with-lease=refs/heads/main:{state_base}", "origin", "HEAD:refs/heads/main"], state, github.env)
    result = {"status": "awaiting_confirmation" if claim["phase"] in {"intake", "plan"} else "ready_for_implementation", "phase": claim["phase"], "state_commit_sha": commit, "constitution_sha": _sha(constitution), "manifest": manifest, "artifact_paths": artifacts, "summary": summary[-1000:]}
    if claim["phase"] == "intake":
        result["intent_context"] = intake_context
    return result
