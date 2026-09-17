"""Prepare an agent-sanitized, compiler-attested public workflow package."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import ConfigError, compile_workflow
from .process import ExecutionError, invoke
from .security import private_path

REQUIREMENTS = Path(".outcomeci/publication-requirements.json")
REPORT = Path(".outcomeci/publication-report.json")
EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PROVIDER_ID = re.compile(r"\b(?:U|W|C|G|T)[A-Z0-9]{8,}\b")
KINDS = {"identity", "vault", "connection", "repository", "endpoint", "provider", "other"}
CONSUMER_KEYS = {
    "channel",
    "channels",
    "connection",
    "connection_ref",
    "credential",
    "credential_ref",
    "participant",
    "participants",
    "provider_id",
    "repositories",
    "repository",
    "target",
    "targets",
    "user",
    "users",
    "workspace_id",
}
GENERIC_VALUES = {"requester", "owner", "builder", "reviewer", "user", "team", "channel"}


def _copy_package(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ExecutionError("publication output directory must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    shutil.copy2(source, target)
    support = source.parent / ".outcomeci"
    if support.is_dir():
        for item in support.rglob("*"):
            relative = item.relative_to(support)
            if not item.is_file() or "outcomes" in relative.parts or private_path(relative):
                continue
            output = destination / ".outcomeci" / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, output)
    return target


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"publication agent did not produce valid {path.name}") from exc


def _validate_requirements(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirements = _json(root / REQUIREMENTS)
    report = _json(root / REPORT)
    if not isinstance(requirements, list) or not isinstance(report, list):
        raise ExecutionError("publication manifests must be JSON arrays")
    identifiers: set[str] = set()
    paths: set[str] = set()
    for item in requirements:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "json_path",
            "kind",
            "description",
            "required",
        }:
            raise ExecutionError("publication requirement has an invalid shape")
        identifier, json_path = item["id"], item["json_path"]
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", identifier)
            or identifier in identifiers
            or not isinstance(json_path, str)
            or not json_path.startswith("$.")
            or json_path in paths
            or item["kind"] not in KINDS
            or not isinstance(item["description"], str)
            or not item["description"].strip()
            or not isinstance(item["required"], bool)
        ):
            raise ExecutionError("publication requirement is invalid or duplicated")
        identifiers.add(identifier)
        paths.add(json_path)
    for item in report:
        if (
            not isinstance(item, dict)
            or set(item) != {"requirement", "files", "reason"}
            or item["requirement"] not in identifiers
            or not isinstance(item["files"], list)
            or not all(isinstance(value, str) for value in item["files"])
            or not isinstance(item["reason"], str)
        ):
            raise ExecutionError("publication replacement report is invalid")
    return requirements, report


def _text_files(root: Path, *, include_manifests: bool = False) -> list[tuple[Path, str]]:
    result = []
    excluded = {root / REQUIREMENTS, root / REPORT}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or (not include_manifests and path in excluded):
            continue
        try:
            result.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return result


def _privacy_gate(root: Path, sensitive_terms: list[str]) -> None:
    failures: set[str] = set()
    terms = [term.strip().casefold() for term in sensitive_terms if term.strip()]
    for path, content in _text_files(root):
        relative = path.relative_to(root).as_posix()
        folded = content.casefold()
        if EMAIL.search(content):
            failures.add(f"{relative}: email address")
        if PROVIDER_ID.search(content):
            failures.add(f"{relative}: provider identifier")
        for term in terms:
            if term in folded:
                failures.add(f"{relative}: configured sensitive term")
    if failures:
        raise ExecutionError(
            "publication privacy verification failed: " + "; ".join(sorted(failures))
        )


def _consumer_values(source: Path) -> list[str]:
    """Extract original values whose exact reuse would make a clone non-portable."""
    try:
        import yaml

        raw = source.read_text(encoding="utf-8")
        document = json.loads(raw) if source.suffix == ".json" else yaml.safe_load(raw)
    except (OSError, ValueError):
        return []
    values: set[str] = set()

    def walk(value: Any, parent: str | None = None) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, str(key).casefold())
        elif isinstance(value, list):
            for child in value:
                walk(child, parent)
        elif isinstance(value, str):
            candidate = value.strip()
            if (
                parent in CONSUMER_KEYS
                or candidate.startswith("vault://")
                or candidate.startswith("https://")
                or candidate.startswith("http://")
                or EMAIL.fullmatch(candidate)
                or PROVIDER_ID.fullmatch(candidate)
            ) and candidate.casefold() not in GENERIC_VALUES:
                values.add(candidate)

    walk(document)
    return sorted(values)


def _package_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path, content in _text_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(content.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_publication(
    source: Path,
    destination: Path,
    *,
    agent: str,
    model: str | None = None,
    sensitive_terms: list[str] | None = None,
    container_isolated: bool = False,
) -> dict[str, Any]:
    original_values = _consumer_values(source.resolve())
    config = _copy_package(source, destination)
    prompt = """Prepare this OutcomeCI workflow package for public reuse.

Edit the copied workflow and its support files in place. Replace every personal or organization-specific value and every value a new consumer must provide: identities, users, groups, channels, email addresses, Vault paths, connection references, repositories, endpoints, provider identifiers, and workspace identifiers. Use safe, valid generic literals so the resulting workflow still compiles. Preserve behavior and never include original sensitive values in your reports.

Create .outcomeci/publication-requirements.json as an array of objects with exactly: id, json_path, kind, description, required. Kinds are identity, vault, connection, repository, endpoint, provider, or other. Create .outcomeci/publication-report.json as an array with exactly: requirement, files, reason. Each report item references a requirement id and contains no original value. Do not modify these contracts. Do not access the network or execute the workflow."""
    invoke(
        agent,
        model,
        prompt,
        destination,
        900,
        writable_paths=[destination],
        excluded_env=set(),
        container_isolated=container_isolated,
    )
    terms = [*(sensitive_terms or []), *original_values]

    def verify():
        requirements, report = _validate_requirements(destination)
        _privacy_gate(destination, terms)
        return requirements, report, compile_workflow(config)

    try:
        requirements, report, compiled = verify()
    except (ExecutionError, ConfigError):
        invoke(
            agent,
            model,
            """The public workflow candidate did not pass OutcomeCI validation. Review the package again, finish replacing every consumer-specific value, repair the workflow so it compiles, and recreate both publication JSON manifests using the exact contracts from the original request. Do not execute the workflow or access the network.""",
            destination,
            900,
            writable_paths=[destination],
            excluded_env=set(),
            container_isolated=container_isolated,
        )
        requirements, report, compiled = verify()
    return {
        "package_digest": _package_digest(destination),
        "workflow_revision": compiled["workflow_revision"],
        "compiler_version": "1",
        "requirements": requirements,
        "replacement_report": report,
        "workflow_file": config.relative_to(destination).as_posix(),
    }
