"""OutcomeCI command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import ConfigError, compile_workflow
from .local import advance as advance_local_outcome
from .local import begin as begin_local_outcome
from .local import compile_context, validate_artifacts
from .local import continue_run as continue_local_outcome
from .local import request_input as request_local_input
from .local import respond as respond_local_outcome
from .local import start as start_local_outcome
from .local import status as local_outcome_status
from .outcome import run as run_outcome
from .process import ExecutionError
from .repository import RepositoryError, initialize, update, validate
from .twin import TwinError, search

AGENT_CHOICES = ("codex", "claude")


def _add_workspace_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--workspace", type=Path, default=Path.cwd())


def _add_workflow_arguments(command: argparse.ArgumentParser, *, agent: bool = False) -> None:
    _add_workspace_argument(command)
    command.add_argument("--config", type=Path)
    if agent:
        command.add_argument("--agent", choices=AGENT_CHOICES)
        command.add_argument("--model")


def _workflow_path(args: argparse.Namespace) -> Path:
    return (args.config or args.workspace / "outcome.yml").resolve()


def _print_json(value: object, *, compact: bool = False, sort_keys: bool = False) -> None:
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    print(json.dumps(value, sort_keys=sort_keys, **options))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="oci", description="Standup and outcome workflows for OutcomeCI"
    )
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
    _add_workflow_arguments(start, agent=True)
    continuation = outcome_commands.add_parser("continue")
    continuation.add_argument("run_id")
    continuation.add_argument("--approve", action="store_true")
    _add_workflow_arguments(continuation, agent=True)
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
    _add_workflow_arguments(respond_command, agent=True)
    twin = commands.add_parser("twin")
    twin_commands = twin.add_subparsers(dest="twin_command", required=True)
    twin_search = twin_commands.add_parser("search")
    twin_search.add_argument("query")
    twin_search.add_argument("--repository-id", action="append", default=[])
    twin_search.add_argument("--limit", type=int, default=10)
    twin_search.add_argument("--component-limit", type=int, default=20)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            _print_json({"created": initialize(args.dir, args.backend)})
        elif args.command == "update":
            _print_json({"created": update(args.dir)})
        elif args.command == "validate":
            result = validate(args.dir)
            _print_json({"valid": True, "workflow_revision": result["workflow_revision"]})
        elif args.command == "status":
            result = validate(args.dir)
            _print_json(
                {
                    "initialized": True,
                    "workflow_revision": result["workflow_revision"],
                    "path": str(args.dir.resolve()),
                }
            )
        elif args.command == "outcome" and args.outcome_command == "validate":
            result = compile_workflow(args.config)
            _print_json(
                {"valid": True, "workflow_revision": result["workflow_revision"]}, sort_keys=True
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
            _print_json(
                start_local_outcome(
                    args.workspace.resolve(),
                    _workflow_path(args),
                    args.intent,
                    agent=args.agent,
                    model=args.model,
                ),
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "continue":
            _print_json(
                continue_local_outcome(
                    args.workspace.resolve(),
                    _workflow_path(args),
                    args.run_id,
                    args.approve,
                    agent=args.agent,
                    model=args.model,
                ),
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "status":
            _print_json(local_outcome_status(args.workspace.resolve(), args.run_id), sort_keys=True)
        elif args.command == "outcome" and args.outcome_command == "begin":
            _print_json(
                begin_local_outcome(args.workspace.resolve(), _workflow_path(args), args.intent),
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "validate-artifacts":
            _print_json(
                validate_artifacts(args.workspace.resolve(), _workflow_path(args), args.run),
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "advance":
            _print_json(
                advance_local_outcome(
                    args.workspace.resolve(), _workflow_path(args), args.run, args.approve
                ),
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "request-input":
            _print_json(
                request_local_input(
                    args.workspace.resolve(), _workflow_path(args), args.run, args.interaction_id
                ),
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "respond":
            _print_json(
                respond_local_outcome(
                    args.workspace.resolve(),
                    _workflow_path(args),
                    args.run,
                    args.interaction_id,
                    args.message,
                    approve=args.approve,
                    reject=args.reject,
                    agent=args.agent,
                    model=args.model,
                ),
                sort_keys=True,
            )
        elif args.command == "twin" and args.twin_command == "search":
            _print_json(
                search(args.query, args.repository_id, args.limit, args.component_limit),
                sort_keys=True,
            )
        return 0
    except (ConfigError, RepositoryError, TwinError, ExecutionError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, ExecutionError) and exc.retryable else 2


if __name__ == "__main__":
    raise SystemExit(main())
