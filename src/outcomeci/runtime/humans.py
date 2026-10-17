"""The people a run can address: the workflow's `humans` resolved to Slack users.

Each declared human is resolved once per run, through the declared api's
lookups, and recorded in the run directory so every step, resume and the
api's wait poller see the same people. A user is its id, an email is looked
up, a group or channel is its members, and a list is the union.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from outcomeci.broker.executor import CredentialResolver, IntegrationError, IntegrationExecutor
from outcomeci.runtime.process import ExecutionError
from outcomeci.security import atomic_write_json

LOOKUPS = ("user_by_email", "usergroups", "usergroup_members", "channels", "members")
USER_ID = re.compile(r"^[UW][A-Za-z0-9_-]+$")
GROUP_ID = re.compile(r"^S[A-Za-z0-9_-]+$")
CHANNEL_ID = re.compile(r"^[CGD][A-Za-z0-9_-]+$")
STEP = "humans"


def path(root: Path, run_id: str) -> Path:
    return root / ".outcomeci" / "outcomes" / run_id / "humans.json"


def load(root: Path, run_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(path(root, run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def resolve(
    root: Path,
    compiled: dict[str, Any],
    state: dict[str, Any],
    resolver: CredentialResolver | None,
) -> dict[str, Any]:
    """Resolve every declared human for this run, once; the record is reused after."""
    declared = compiled.get("humans") or {}
    if not declared:
        return {}
    found = load(root, state["run_id"])
    if found is not None:
        return found
    if resolver is None:
        raise ExecutionError("resolving humans requires a credential resolver")
    executor = IntegrationExecutor(_with_lookups(compiled), resolver=resolver, reviewed=True)
    resolved: dict[str, Any] = {}
    for name, spec in declared.items():
        if spec["type"] == "list":
            users: list[str] = []
            members = []
            for member in spec["members"]:
                record = (
                    resolved[member["human"]]
                    if "human" in member
                    else _principal(root, state, executor, member, name)
                )
                members.append(member.get("human") or record)
                users.extend(user for user in record["users"] if user not in users)
            resolved[name] = {"type": "list", "members": members, "users": users}
        else:
            resolved[name] = _principal(root, state, executor, spec, name)
    atomic_write_json(path(root, state["run_id"]), resolved)
    return resolved


def _with_lookups(compiled: dict[str, Any]) -> dict[str, Any]:
    """The compiled workflow with a runtime-only step allowed the lookups."""
    capabilities = sorted(
        f"{api}.{operation}"
        for api, binding in (compiled.get("connectors") or {}).items()
        if binding.get("provider") == "slack"
        for operation in LOOKUPS
        if operation in compiled["workflow"]["spec"]["integrations"][api]["operations"]
    )
    steps = {**compiled["instructions"]["steps"], STEP: {"capabilities": capabilities}}
    return {**compiled, "instructions": {**compiled["instructions"], "steps": steps}}


def _principal(root, state, executor, spec: dict[str, Any], name: str) -> dict[str, Any]:
    from outcomeci.runtime.steps import value

    identifier = spec["identifier"]
    if "ref" in identifier:
        found = value(root, state, identifier["ref"])
        if not isinstance(found, str) or not found.strip():
            raise ExecutionError(f"humans.{name}: {identifier['ref']} did not name a person")
        literal = found.strip()
    else:
        literal = identifier["literal"]
    api, kind = spec["system"], spec["type"]
    record = {"type": kind, "system": api, "identifier": literal}

    def call(operation: str, inputs: dict[str, Any]) -> dict[str, Any]:
        try:
            return executor.execute(f"{api}.{operation}", inputs, step=STEP).get("output") or {}
        except IntegrationError as exc:
            raise ExecutionError(f"humans.{name}: {api}.{operation} failed: {exc}") from exc

    if kind == "user":
        if not USER_ID.fullmatch(literal):
            raise ExecutionError(
                f"humans.{name}: a user is named by a Slack user id; use type email for an address"
            )
        return {**record, "id": literal, "users": [literal]}
    if kind == "email":
        try:
            user = call("user_by_email", {"email": literal}).get("id")
        except ExecutionError as exc:
            if "users_not_found" not in str(exc):
                raise
            user = None
        if not isinstance(user, str) or not user:
            raise ExecutionError(f"humans.{name}: no Slack user has the address {literal}")
        return {**record, "id": user, "users": [user]}
    if kind == "group":
        group = literal
        if literal.startswith("@"):
            handle = literal[1:]
            group = next(
                (
                    item.get("id")
                    for item in call("usergroups", {}).get("usergroups") or []
                    if isinstance(item, dict) and item.get("handle") == handle
                ),
                None,
            )
        if not isinstance(group, str) or not GROUP_ID.fullmatch(group):
            raise ExecutionError(f"humans.{name}: no user group is called {literal}")
        users = call("usergroup_members", {"usergroup": group}).get("users") or []
        return {**record, "id": group, "users": _users(users, name)}
    channel = literal
    if literal.startswith("#"):
        wanted = literal[1:]
        channel = next(
            (
                item.get("id")
                for item in call("channels", {}).get("channels") or []
                if isinstance(item, dict) and item.get("name") == wanted
            ),
            None,
        )
    if not isinstance(channel, str) or not CHANNEL_ID.fullmatch(channel):
        raise ExecutionError(f"humans.{name}: no channel is called {literal}")
    users = call("members", {"channel": channel}).get("members") or []
    return {**record, "id": channel, "users": _users(users, name)}


def _users(found: Any, name: str) -> list[str]:
    users = [item for item in found if isinstance(item, str) and USER_ID.fullmatch(item)]
    if not users:
        raise ExecutionError(f"humans.{name} has no members")
    return sorted(users)


def names(users: list[str], limit: int = 12) -> str:
    """People as Slack mentions, shortened when a group is large."""
    shown = ", ".join(f"<@{user}>" for user in users[:limit])
    rest = len(users) - limit
    return shown + (f" and {rest} more" if rest > 0 else "")
