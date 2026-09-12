"""OutcomeCI command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .capability import invoke as invoke_capability
from .cloud import auth_status as cloud_auth_status
from .cloud import login as cloud_login
from .cloud import login_with_key as cloud_login_with_key
from .cloud import logout as cloud_logout
from .cloud import sync_workflow, vault_request
from .config import ConfigError, compile_workflow
from .humans import accept as accept_human_input
from .humans import assign as assign_human_hook
from .humans import poll as poll_human_input
from .humans import request as request_human_input
from .local import advance as advance_local_outcome
from .local import begin as begin_local_outcome
from .local import compile_context, validate_artifacts
from .local import continue_run as continue_local_outcome
from .local import recover as recover_local_outcome
from .local import request_input as request_local_input
from .local import respond as respond_local_outcome
from .local import retry as retry_local_outcome
from .local import start as start_local_outcome
from .local import status as local_outcome_status
from .outcome import run as run_outcome
from .process import ExecutionError
from .repository import RepositoryError, initialize, update, validate
from .slack import SlackError
from .slack import manifest as slack_manifest
from .slack import setup as setup_slack
from .slack import status as slack_status
from .slack import targets as slack_targets
from .twin import TwinError, search

AGENT_CHOICES = ("codex", "claude")


def _add_workspace_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--workspace", type=Path, default=Path.cwd())


def _add_workflow_arguments(
    command: argparse.ArgumentParser, *, agent_overrides: bool = False
) -> None:
    _add_workspace_argument(command)
    command.add_argument("--config", type=Path)
    if agent_overrides:
        command.add_argument("--agent", choices=AGENT_CHOICES)
        command.add_argument("--model")


def _workflow_path(args: argparse.Namespace) -> Path:
    return (args.config or args.workspace / "outcome.yml").resolve()


def _print_json(value: object, *, compact: bool = False, sort_keys: bool = False) -> None:
    options = {"separators": (",", ":")} if compact else {"indent": 2, "sort_keys": sort_keys}
    print(json.dumps(value, default=str, **options))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="oci", description="Standup and outcome workflows for OutcomeCI"
    )
    root.add_argument("--version", action="version", version=f"oci {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Authenticate with OutcomeCI Cloud")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_login = auth_commands.add_parser("login")
    auth_login.add_argument(
        "--api-url", default=os.environ.get("OUTCOMECI_API_URL", "https://api.outcomeci.com")
    )
    auth_login.add_argument("--no-open", action="store_true")
    auth_login.add_argument(
        "--key-stdin",
        action="store_true",
        help="Read a workspace API key from stdin instead of using device login",
    )
    auth_commands.add_parser("status")
    auth_commands.add_parser("logout")
    workflow = commands.add_parser("workflow", help="Manage OutcomeCI Cloud workflows")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_sync = workflow_commands.add_parser("sync")
    workflow_sync.add_argument("file", type=Path)
    workflow_sync.add_argument("--workspace", required=True)
    workflow_sync.add_argument("--name")
    workflow_mode = workflow_sync.add_mutually_exclusive_group(required=True)
    workflow_mode.add_argument("--create", action="store_true")
    workflow_mode.add_argument("--version", action="store_true")
    vault = commands.add_parser(
        "vault", help="Manage workspace credentials through OutcomeCI Vault"
    )
    vault_commands = vault.add_subparsers(dest="vault_command", required=True)
    vault_list = vault_commands.add_parser("list")
    vault_list.add_argument("--workspace", required=True)
    vault_put = vault_commands.add_parser("put")
    vault_put.add_argument("path")
    vault_put.add_argument("--workspace", required=True)
    vault_put.add_argument("--name")
    vault_put.add_argument("--value")
    vault_put.add_argument("--value-stdin", action="store_true")
    vault_put.add_argument("--workflow", action="append", default=[])
    vault_rotate = vault_commands.add_parser("rotate")
    vault_rotate.add_argument("entry_id")
    vault_rotate.add_argument("--workspace", required=True)
    vault_rotate.add_argument("--value")
    vault_rotate.add_argument("--value-stdin", action="store_true")
    vault_grant = vault_commands.add_parser("grant")
    vault_grant.add_argument("entry_id")
    vault_grant.add_argument("--workspace", required=True)
    vault_grant.add_argument("--workflow", action="append", default=[])
    vault_revoke = vault_commands.add_parser("revoke")
    vault_revoke.add_argument("entry_id")
    vault_revoke.add_argument("--workspace", required=True)
    for name in ("init", "update", "validate", "status"):
        item = commands.add_parser(name)
        item.add_argument("--dir", type=Path, default=Path.cwd())
        if name == "init":
            item.add_argument("--backend", choices=("outcomeci", "filesystem"), default="outcomeci")
    outcome = commands.add_parser("outcome")
    outcome_commands = outcome.add_subparsers(dest="outcome_command", required=True)
    for name in ("validate", "compile"):
        item = outcome_commands.add_parser(name)
        item.add_argument("config", nargs="?", type=Path, default=Path("outcome.yml"))
        if name == "compile":
            item.add_argument("--phase")
            item.add_argument("--run")
            item.add_argument("--workspace", type=Path, default=Path.cwd())
    run = outcome_commands.add_parser("run")
    run.add_argument("--claim", required=True, type=Path)
    run.add_argument("--workspace", type=Path, default=Path("/workspace"))
    start = outcome_commands.add_parser("start")
    start.add_argument("intent")
    _add_workflow_arguments(start, agent_overrides=True)
    continuation = outcome_commands.add_parser("continue")
    continuation.add_argument("run_id")
    continuation.add_argument("--approve", action="store_true")
    _add_workflow_arguments(continuation, agent_overrides=True)
    retry_command = outcome_commands.add_parser("retry")
    retry_command.add_argument("run_id")
    _add_workflow_arguments(retry_command, agent_overrides=True)
    recover_command = outcome_commands.add_parser("recover")
    recover_command.add_argument("run_id")
    _add_workflow_arguments(recover_command)
    outcome_status = outcome_commands.add_parser("status")
    outcome_status.add_argument("run_id", nargs="?")
    _add_workspace_argument(outcome_status)
    begin_command = outcome_commands.add_parser("begin")
    begin_command.add_argument("intent")
    _add_workflow_arguments(begin_command)
    validate_command = outcome_commands.add_parser("validate-artifacts")
    validate_command.add_argument("--run")
    _add_workflow_arguments(validate_command)
    advance_command = outcome_commands.add_parser("advance")
    advance_command.add_argument("--run")
    advance_command.add_argument("--approve", action="store_true")
    _add_workflow_arguments(advance_command)
    request_command = outcome_commands.add_parser("request-input")
    request_command.add_argument("interaction_id")
    request_command.add_argument("--run", required=True)
    _add_workflow_arguments(request_command)
    respond_command = outcome_commands.add_parser("respond")
    respond_command.add_argument("interaction_id")
    respond_command.add_argument("message")
    respond_command.add_argument("--run", required=True)
    respond_command.add_argument("--approve", action="store_true")
    respond_command.add_argument("--reject", action="store_true")
    _add_workflow_arguments(respond_command, agent_overrides=True)
    human = commands.add_parser("human")
    human_commands = human.add_subparsers(dest="human_command", required=True)
    human_targets = human_commands.add_parser(
        "targets", help="List readable Slack people, channels, and groups"
    )
    _add_workspace_argument(human_targets)
    human_assign = human_commands.add_parser(
        "assign", help="Assign readable targets to a workflow hook"
    )
    human_assign.add_argument("phase")
    human_assign.add_argument("timing", choices=("before", "during", "after"))
    human_assign.add_argument("interaction_id")
    human_assign.add_argument("--user", action="append", default=[])
    human_assign.add_argument("--channel", action="append", default=[])
    human_assign.add_argument("--group", action="append", default=[])
    human_assign.add_argument("--wait", choices=("ask", "block", "continue"), default="ask")
    human_assign.add_argument("--timeout", type=int)
    human_assign.add_argument("--connection", default="slack_local")
    _add_workflow_arguments(human_assign)
    human_request = human_commands.add_parser("request", help="Deliver a configured human hook")
    human_request.add_argument("interaction_id")
    human_request.add_argument("--run", required=True)
    _add_workflow_arguments(human_request)
    human_request.add_argument("--continue", dest="continue_while_waiting", action="store_true")
    human_poll = human_commands.add_parser("poll", help="Poll Slack for human responses")
    human_poll.add_argument("interaction_id")
    human_poll.add_argument("--run", required=True)
    human_poll.add_argument("--wait", type=int, default=0)
    human_poll.add_argument("--interval", type=float, default=2)
    _add_workflow_arguments(human_poll)
    human_accept = human_commands.add_parser(
        "accept", help="Persist a polled response without launching another agent"
    )
    human_accept.add_argument("interaction_id")
    human_accept.add_argument("message")
    human_accept.add_argument("--run", required=True)
    human_accept.add_argument("--approve", action="store_true")
    human_accept.add_argument("--reject", action="store_true")
    _add_workflow_arguments(human_accept)
    twin = commands.add_parser("twin")
    twin_commands = twin.add_subparsers(dest="twin_command", required=True)
    twin_search = twin_commands.add_parser("search")
    twin_search.add_argument("query")
    twin_search.add_argument("--repository-id", action="append", default=[])
    twin_search.add_argument("--limit", type=int, default=10)
    twin_search.add_argument("--component-limit", type=int, default=20)
    integration = commands.add_parser("integration")
    integration_commands = integration.add_subparsers(dest="integration_command", required=True)
    slack = integration_commands.add_parser("slack")
    slack_commands = slack.add_subparsers(dest="slack_command", required=True)
    slack_setup = slack_commands.add_parser("setup")
    _add_workspace_argument(slack_setup)
    slack_setup.add_argument("--name", default="OutcomeCI")
    slack_setup.add_argument("--team")
    slack_setup.add_argument("--channel", help="Default Slack channel or user ID for human hooks")
    slack_setup.add_argument("--force", action="store_true")
    slack_status_command = slack_commands.add_parser("status")
    _add_workspace_argument(slack_status_command)
    slack_targets_command = slack_commands.add_parser("targets", help="List readable Slack targets")
    _add_workspace_argument(slack_targets_command)
    slack_manifest_command = slack_commands.add_parser(
        "manifest", help="Print the generated Slack app manifest"
    )
    slack_manifest_command.add_argument("--project", type=Path, default=Path.cwd())
    slack_manifest_command.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "auth":
            if args.auth_command == "login":
                if args.key_stdin:
                    key = (
                        getpass.getpass("Workspace API key: ")
                        if sys.stdin.isatty()
                        else sys.stdin.read()
                    )
                    _print_json(cloud_login_with_key(args.api_url, key))
                else:
                    _print_json(cloud_login(args.api_url, open_browser=not args.no_open))
            elif args.auth_command == "status":
                _print_json(cloud_auth_status())
            else:
                _print_json(cloud_logout())
            return 0
        if args.command == "workflow":
            result = sync_workflow(
                args.file, args.workspace, args.name, "create" if args.create else "version"
            )
            _print_json(result)
            return 0
        if args.command == "vault":
            if args.vault_command == "list":
                result = vault_request(args.workspace, "list")
            elif args.vault_command in {"put", "rotate"}:
                value = sys.stdin.read().rstrip("\n") if args.value_stdin else args.value
                if not value:
                    raise ExecutionError("secret value is required; use --value-stdin or --value")
                if args.vault_command == "put":
                    result = vault_request(
                        args.workspace,
                        "put",
                        path=args.path,
                        display_name=args.name or args.path,
                        value=value,
                        workflow_ids=args.workflow,
                    )
                else:
                    result = vault_request(
                        args.workspace, "rotate", entry_id=args.entry_id, value=value
                    )
            elif args.vault_command == "grant":
                result = vault_request(
                    args.workspace, "grant", entry_id=args.entry_id, workflow_ids=args.workflow
                )
            else:
                result = vault_request(args.workspace, "revoke", entry_id=args.entry_id)
            _print_json(result or {"ok": True})
            return 0
        if args.command == "init":
            _print_json({"created": initialize(args.dir, args.backend)})
        elif args.command == "update":
            _print_json({"created": update(args.dir)})
        elif args.command == "validate":
            result = validate(args.dir)
            print(
                json.dumps(
                    {"valid": True, "workflow_revision": result["workflow_revision"]}, indent=2
                )
            )
        elif args.command == "status":
            result = validate(args.dir)
            print(
                json.dumps(
                    {
                        "initialized": True,
                        "workflow_revision": result["workflow_revision"],
                        "path": str(args.dir.resolve()),
                    },
                    indent=2,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "validate":
            result = compile_workflow(args.config)
            print(
                json.dumps(
                    {"valid": True, "workflow_revision": result["workflow_revision"]},
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "compile":
            if args.run:
                result = compile_context(args.workspace.resolve(), args.config.resolve(), args.run)
            else:
                result = compile_workflow(args.config)
                if args.phase:
                    phase = result["instructions"]["phases"].get(args.phase)
                    if phase is None:
                        raise ExecutionError(f"workflow has no instructions for {args.phase}")
                    result = {
                        **result,
                        "instructions": {
                            "orchestrator": result["instructions"]["orchestrator"],
                            "phase": phase,
                        },
                    }
            _print_json(result, sort_keys=True)
        elif args.command == "outcome" and args.outcome_command == "run":
            _print_json(run_outcome(args.claim, args.workspace), compact=True)
        elif args.command == "outcome" and args.outcome_command == "start":
            config = _workflow_path(args)
            print(
                json.dumps(
                    start_local_outcome(
                        args.workspace.resolve(),
                        config,
                        args.intent,
                        agent=args.agent,
                        model=args.model,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "continue":
            config = _workflow_path(args)
            print(
                json.dumps(
                    continue_local_outcome(
                        args.workspace.resolve(),
                        config,
                        args.run_id,
                        args.approve,
                        agent=args.agent,
                        model=args.model,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "retry":
            config = _workflow_path(args)
            print(
                json.dumps(
                    retry_local_outcome(
                        args.workspace.resolve(),
                        config,
                        args.run_id,
                        agent=args.agent,
                        model=args.model,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "recover":
            config = _workflow_path(args)
            print(
                json.dumps(
                    recover_local_outcome(args.workspace.resolve(), config, args.run_id),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "status":
            print(
                json.dumps(
                    local_outcome_status(args.workspace.resolve(), args.run_id),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "begin":
            config = _workflow_path(args)
            print(
                json.dumps(
                    begin_local_outcome(args.workspace.resolve(), config, args.intent),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "validate-artifacts":
            config = _workflow_path(args)
            print(
                json.dumps(
                    validate_artifacts(args.workspace.resolve(), config, args.run),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "advance":
            config = _workflow_path(args)
            print(
                json.dumps(
                    advance_local_outcome(args.workspace.resolve(), config, args.run, args.approve),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "request-input":
            config = _workflow_path(args)
            print(
                json.dumps(
                    request_local_input(
                        args.workspace.resolve(), config, args.run, args.interaction_id
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "outcome" and args.outcome_command == "respond":
            config = _workflow_path(args)
            print(
                json.dumps(
                    respond_local_outcome(
                        args.workspace.resolve(),
                        config,
                        args.run,
                        args.interaction_id,
                        args.message,
                        approve=args.approve,
                        reject=args.reject,
                        agent=args.agent,
                        model=args.model,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "human":
            workspace = args.workspace.resolve()
            scoped = bool(os.environ.get("OUTCOMECI_CAPABILITY_SOCKET"))
            if args.human_command == "targets":
                if scoped:
                    raise ExecutionError("target discovery is not allowed during outcome execution")
                _print_json(slack_targets(workspace), sort_keys=True)
            elif args.human_command == "assign":
                if scoped:
                    raise ExecutionError("hook assignment is not allowed during outcome execution")
                config = (args.config or workspace / "outcome.yml").resolve()
                selected = (
                    [("user", value) for value in args.user]
                    + [("channel", value) for value in args.channel]
                    + [("group", value) for value in args.group]
                )
                if not selected:
                    raise ExecutionError("assign at least one --user, --channel, or --group")
                print(
                    json.dumps(
                        assign_human_hook(
                            workspace,
                            config,
                            args.phase,
                            args.timing,
                            args.interaction_id,
                            selected,
                            args.wait,
                            args.timeout,
                            args.connection,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
            elif args.human_command == "request":
                result = (
                    invoke_capability(
                        "request",
                        args.run,
                        args.interaction_id,
                        continue_while_waiting=args.continue_while_waiting,
                    )
                    if scoped
                    else request_human_input(
                        workspace,
                        (args.config or workspace / "outcome.yml").resolve(),
                        args.run,
                        args.interaction_id,
                        args.continue_while_waiting,
                    )
                )
                _print_json(result, sort_keys=True)
            elif args.human_command == "poll":
                config = (getattr(args, "config", None) or workspace / "outcome.yml").resolve()
                result = (
                    invoke_capability(
                        "poll",
                        args.run,
                        args.interaction_id,
                        wait_seconds=args.wait,
                        interval_seconds=args.interval,
                    )
                    if scoped
                    else poll_human_input(
                        workspace, config, args.run, args.interaction_id, args.wait, args.interval
                    )
                )
                _print_json(result, sort_keys=True)
            elif args.human_command == "accept":
                result = (
                    invoke_capability(
                        "accept",
                        args.run,
                        args.interaction_id,
                        message=args.message,
                        approve=args.approve,
                        reject=args.reject,
                    )
                    if scoped
                    else accept_human_input(
                        workspace,
                        (args.config or workspace / "outcome.yml").resolve(),
                        args.run,
                        args.interaction_id,
                        args.message,
                        args.approve,
                        args.reject,
                    )
                )
                _print_json(result, sort_keys=True)
        elif args.command == "twin" and args.twin_command == "search":
            print(
                json.dumps(
                    search(args.query, args.repository_id, args.limit, args.component_limit),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "integration" and args.integration_command == "slack":
            if args.slack_command == "setup":
                print(
                    json.dumps(
                        setup_slack(
                            args.workspace,
                            name=args.name,
                            team=args.team,
                            channel=args.channel,
                            force=args.force,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
            elif args.slack_command == "status":
                _print_json(slack_status(args.workspace), sort_keys=True)
            elif args.slack_command == "targets":
                _print_json(slack_targets(args.workspace.resolve()), sort_keys=True)
            elif args.slack_command == "manifest":
                _print_json(slack_manifest(args.source or args.project), compact=True)
        return 0
    except (ConfigError, RepositoryError, TwinError, ExecutionError, SlackError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, ExecutionError) and exc.retryable else 2


if __name__ == "__main__":
    raise SystemExit(main())
