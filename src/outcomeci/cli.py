"""OutcomeCI command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, compile_workflow
from .capability import invoke as invoke_capability
from .humans import accept as accept_human_input
from .humans import assign as assign_human_hook
from .humans import poll as poll_human_input
from .humans import request as request_human_input
from .local import advance as advance_local_outcome
from .local import begin as begin_local_outcome
from .local import compile_context
from .local import continue_run as continue_local_outcome
from .local import start as start_local_outcome
from .local import status as local_outcome_status
from .local import validate_artifacts
from .local import request_input as request_local_input
from .local import recover as recover_local_outcome
from .local import retry as retry_local_outcome
from .local import respond as respond_local_outcome
from .outcome import run as run_outcome
from .process import ExecutionError
from .repository import RepositoryError, initialize, update, validate
from .slack import SlackError
from .slack import manifest as slack_manifest
from .slack import setup as setup_slack
from .slack import status as slack_status
from .slack import targets as slack_targets
from .twin import TwinError, search


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="oci", description="Standup and outcome workflows for OutcomeCI")
    root.add_argument("--version", action="version", version=f"oci {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
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
    start.add_argument("--workspace", type=Path, default=Path.cwd())
    start.add_argument("--config", type=Path)
    start.add_argument("--agent", choices=("codex", "claude"))
    start.add_argument("--model")
    continuation = outcome_commands.add_parser("continue")
    continuation.add_argument("run_id")
    continuation.add_argument("--approve", action="store_true")
    continuation.add_argument("--workspace", type=Path, default=Path.cwd())
    continuation.add_argument("--config", type=Path)
    continuation.add_argument("--agent", choices=("codex", "claude"))
    continuation.add_argument("--model")
    retry_command = outcome_commands.add_parser("retry")
    retry_command.add_argument("run_id")
    retry_command.add_argument("--workspace", type=Path, default=Path.cwd())
    retry_command.add_argument("--config", type=Path)
    retry_command.add_argument("--agent", choices=("codex", "claude"))
    retry_command.add_argument("--model")
    recover_command = outcome_commands.add_parser("recover")
    recover_command.add_argument("run_id")
    recover_command.add_argument("--workspace", type=Path, default=Path.cwd())
    recover_command.add_argument("--config", type=Path)
    outcome_status = outcome_commands.add_parser("status")
    outcome_status.add_argument("run_id", nargs="?")
    outcome_status.add_argument("--workspace", type=Path, default=Path.cwd())
    begin_command = outcome_commands.add_parser("begin")
    begin_command.add_argument("intent")
    begin_command.add_argument("--workspace", type=Path, default=Path.cwd())
    begin_command.add_argument("--config", type=Path)
    validate_command = outcome_commands.add_parser("validate-artifacts")
    validate_command.add_argument("--run")
    validate_command.add_argument("--workspace", type=Path, default=Path.cwd())
    validate_command.add_argument("--config", type=Path)
    advance_command = outcome_commands.add_parser("advance")
    advance_command.add_argument("--run")
    advance_command.add_argument("--approve", action="store_true")
    advance_command.add_argument("--workspace", type=Path, default=Path.cwd())
    advance_command.add_argument("--config", type=Path)
    request_command = outcome_commands.add_parser("request-input")
    request_command.add_argument("interaction_id")
    request_command.add_argument("--run", required=True)
    request_command.add_argument("--workspace", type=Path, default=Path.cwd())
    request_command.add_argument("--config", type=Path)
    respond_command = outcome_commands.add_parser("respond")
    respond_command.add_argument("interaction_id")
    respond_command.add_argument("message")
    respond_command.add_argument("--run", required=True)
    respond_command.add_argument("--approve", action="store_true")
    respond_command.add_argument("--reject", action="store_true")
    respond_command.add_argument("--workspace", type=Path, default=Path.cwd())
    respond_command.add_argument("--config", type=Path)
    respond_command.add_argument("--agent", choices=("codex", "claude"))
    respond_command.add_argument("--model")
    human = commands.add_parser("human")
    human_commands = human.add_subparsers(dest="human_command", required=True)
    human_targets = human_commands.add_parser("targets", help="List readable Slack people, channels, and groups")
    human_targets.add_argument("--workspace", type=Path, default=Path.cwd())
    human_assign = human_commands.add_parser("assign", help="Assign Slack targets to a workflow hook")
    human_assign.add_argument("phase")
    human_assign.add_argument("timing", choices=("before", "during", "after"))
    human_assign.add_argument("interaction_id")
    human_assign.add_argument("--user", action="append", default=[])
    human_assign.add_argument("--channel", action="append", default=[])
    human_assign.add_argument("--group", action="append", default=[])
    human_assign.add_argument("--wait", choices=("ask", "block", "continue"), default="ask")
    human_assign.add_argument("--timeout", type=int)
    human_assign.add_argument("--workspace", type=Path, default=Path.cwd())
    human_assign.add_argument("--config", type=Path)
    human_request = human_commands.add_parser("request", help="Deliver a configured human hook")
    human_request.add_argument("interaction_id")
    human_request.add_argument("--run", required=True)
    human_request.add_argument("--workspace", type=Path, default=Path.cwd())
    human_request.add_argument("--config", type=Path)
    human_request.add_argument("--continue", dest="continue_while_waiting", action="store_true")
    human_poll = human_commands.add_parser("poll", help="Poll Slack for human responses")
    human_poll.add_argument("interaction_id")
    human_poll.add_argument("--run", required=True)
    human_poll.add_argument("--wait", type=int, default=0)
    human_poll.add_argument("--interval", type=float, default=2)
    human_poll.add_argument("--workspace", type=Path, default=Path.cwd())
    human_accept = human_commands.add_parser("accept", help="Persist a polled response without launching another agent")
    human_accept.add_argument("interaction_id")
    human_accept.add_argument("message")
    human_accept.add_argument("--run", required=True)
    human_accept.add_argument("--approve", action="store_true")
    human_accept.add_argument("--reject", action="store_true")
    human_accept.add_argument("--workspace", type=Path, default=Path.cwd())
    human_accept.add_argument("--config", type=Path)
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
    slack_setup.add_argument("--workspace", type=Path, default=Path.cwd())
    slack_setup.add_argument("--name", default="OutcomeCI")
    slack_setup.add_argument("--team")
    slack_setup.add_argument("--channel", help="Default Slack channel or user ID for human hooks")
    slack_setup.add_argument("--force", action="store_true")
    slack_status_command = slack_commands.add_parser("status")
    slack_status_command.add_argument("--workspace", type=Path, default=Path.cwd())
    slack_targets_command = slack_commands.add_parser("targets", help="List readable Slack targets")
    slack_targets_command.add_argument("--workspace", type=Path, default=Path.cwd())
    slack_manifest_command = slack_commands.add_parser("manifest", help="Print the generated Slack app manifest")
    slack_manifest_command.add_argument("--project", type=Path, default=Path.cwd())
    slack_manifest_command.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            print(json.dumps({"created": initialize(args.dir, args.backend)}, indent=2))
        elif args.command == "update":
            print(json.dumps({"created": update(args.dir)}, indent=2))
        elif args.command == "validate":
            result = validate(args.dir)
            print(json.dumps({"valid": True, "workflow_revision": result["workflow_revision"]}, indent=2))
        elif args.command == "status":
            result = validate(args.dir)
            print(json.dumps({"initialized": True, "workflow_revision": result["workflow_revision"], "path": str(args.dir.resolve())}, indent=2))
        elif args.command == "outcome" and args.outcome_command == "validate":
            result = compile_workflow(args.config)
            print(json.dumps({"valid": True, "workflow_revision": result["workflow_revision"]}, indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "compile":
            if args.run:
                result = compile_context(args.workspace.resolve(), args.config.resolve(), args.run)
            else:
                result = compile_workflow(args.config)
                if args.phase:
                    phase = result["instructions"]["phases"].get(args.phase)
                    if phase is None:
                        raise ExecutionError(f"workflow has no instructions for {args.phase}")
                    result = {**result, "instructions": {"orchestrator": result["instructions"]["orchestrator"], "phase": phase}}
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "run":
            print(json.dumps(run_outcome(args.claim, args.workspace), separators=(",", ":")))
        elif args.command == "outcome" and args.outcome_command == "start":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(start_local_outcome(args.workspace.resolve(), config.resolve(), args.intent, agent=args.agent, model=args.model), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "continue":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(continue_local_outcome(args.workspace.resolve(), config.resolve(), args.run_id, args.approve, agent=args.agent, model=args.model), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "retry":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(retry_local_outcome(args.workspace.resolve(), config.resolve(), args.run_id, agent=args.agent, model=args.model), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "recover":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(recover_local_outcome(args.workspace.resolve(), config.resolve(), args.run_id), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "status":
            print(json.dumps(local_outcome_status(args.workspace.resolve(), args.run_id), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "begin":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(begin_local_outcome(args.workspace.resolve(), config.resolve(), args.intent), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "validate-artifacts":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(validate_artifacts(args.workspace.resolve(), config.resolve(), args.run), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "advance":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(advance_local_outcome(args.workspace.resolve(), config.resolve(), args.run, args.approve), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "request-input":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(request_local_input(args.workspace.resolve(), config.resolve(), args.run, args.interaction_id), indent=2, sort_keys=True))
        elif args.command == "outcome" and args.outcome_command == "respond":
            config = args.config or args.workspace / "outcome.yml"
            print(json.dumps(respond_local_outcome(args.workspace.resolve(), config.resolve(), args.run, args.interaction_id, args.message, approve=args.approve, reject=args.reject, agent=args.agent, model=args.model), indent=2, sort_keys=True))
        elif args.command == "human":
            workspace = args.workspace.resolve()
            scoped = bool(os.environ.get("OUTCOMECI_CAPABILITY_SOCKET"))
            if args.human_command == "targets":
                if scoped:
                    raise ExecutionError("target discovery is not allowed during outcome execution")
                print(json.dumps(slack_targets(workspace), indent=2, sort_keys=True))
            elif args.human_command == "assign":
                if scoped:
                    raise ExecutionError("hook assignment is not allowed during outcome execution")
                config = (args.config or workspace / "outcome.yml").resolve()
                selected = [("user", value) for value in args.user] + [("channel", value) for value in args.channel] + [("group", value) for value in args.group]
                if not selected:
                    raise ExecutionError("assign at least one --user, --channel, or --group")
                print(json.dumps(assign_human_hook(workspace, config, args.phase, args.timing, args.interaction_id, selected, args.wait, args.timeout), indent=2, sort_keys=True))
            elif args.human_command == "request":
                result = invoke_capability("request", args.run, args.interaction_id, continue_while_waiting=args.continue_while_waiting) if scoped else request_human_input(workspace, (args.config or workspace / "outcome.yml").resolve(), args.run, args.interaction_id, args.continue_while_waiting)
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.human_command == "poll":
                result = invoke_capability("poll", args.run, args.interaction_id, wait_seconds=args.wait, interval_seconds=args.interval) if scoped else poll_human_input(workspace, args.run, args.interaction_id, args.wait, args.interval)
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.human_command == "accept":
                result = invoke_capability("accept", args.run, args.interaction_id, message=args.message, approve=args.approve, reject=args.reject) if scoped else accept_human_input(workspace, (args.config or workspace / "outcome.yml").resolve(), args.run, args.interaction_id, args.message, args.approve, args.reject)
                print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "twin" and args.twin_command == "search":
            print(json.dumps(search(args.query, args.repository_id, args.limit, args.component_limit), indent=2, sort_keys=True))
        elif args.command == "integration" and args.integration_command == "slack":
            if args.slack_command == "setup":
                print(json.dumps(setup_slack(args.workspace, name=args.name, team=args.team, channel=args.channel, force=args.force), indent=2, sort_keys=True))
            elif args.slack_command == "status":
                print(json.dumps(slack_status(args.workspace), indent=2, sort_keys=True))
            elif args.slack_command == "targets":
                print(json.dumps(slack_targets(args.workspace.resolve()), indent=2, sort_keys=True))
            elif args.slack_command == "manifest":
                print(json.dumps(slack_manifest(args.source or args.project), separators=(",", ":")))
        return 0
    except (ConfigError, RepositoryError, TwinError, ExecutionError, SlackError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, ExecutionError) and exc.retryable else 2


if __name__ == "__main__":
    raise SystemExit(main())
