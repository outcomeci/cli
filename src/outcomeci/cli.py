"""OutcomeCI command-line interface."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from outcomeci_connectors.providers.slack.setup import SlackError
from outcomeci_connectors.providers.slack.setup import manifest as slack_manifest
from outcomeci_connectors.providers.slack.setup import setup as setup_slack
from outcomeci_connectors.providers.slack.setup import status as slack_status

from . import __version__, credentials, mcp_setup, slack_vault, workflow_run
from .capability import invoke_integration
from .cloud import auth_status as cloud_auth_status
from .cloud import credentials_path as cloud_credentials_path
from .cloud import get_workflow, sync_workflow, vault_request
from .cloud import login as cloud_login
from .cloud import login_with_key as cloud_login_with_key
from .cloud import logout as cloud_logout
from .config import ConfigError, compile_workflow
from .integrations import (
    IntegrationError,
    IntegrationExecutor,
    doctor,
    local_credential_resolver,
)
from .local_vault import initialize as initialize_local_vault
from .local_vault import list_entries as list_local_vault_entries
from .local_vault import put as put_local_vault_entry
from .process import ExecutionError
from .publication import prepare_publication
from .repository import initialize

AGENT_CHOICES = ("codex", "claude", "opencode")


def _secret_input(args: argparse.Namespace, *, prompt: bool) -> tuple[str | None, dict | None]:
    """The secret a put stores: one value, or a JSON object of secret fields."""
    if args.secrets_json_stdin:
        try:
            secrets = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            raise ExecutionError("--secrets-json-stdin requires a JSON object") from exc
        if not isinstance(secrets, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in secrets.items()
        ):
            raise ExecutionError("--secrets-json-stdin requires string fields")
        return None, secrets
    if args.value_stdin:
        value = sys.stdin.read().rstrip("\n")
    elif args.value is not None:
        value = args.value
    elif prompt:
        value = getpass.getpass("Secret value: ")
    else:
        value = ""
    if not value:
        raise ExecutionError("secret value is required; use --value-stdin or --value")
    return value, None


def _add_dir_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--dir", type=Path, default=Path.cwd())


def _add_workflow_arguments(command: argparse.ArgumentParser) -> None:
    _add_dir_argument(command)
    command.add_argument("--config", type=Path)


def _workflow_path(args: argparse.Namespace) -> Path:
    return (args.dir / (args.config or "outcome.yml")).resolve()


def _print_json(value: object, *, compact: bool = False, sort_keys: bool = False) -> None:
    options = {"separators": (",", ":")} if compact else {"indent": 2, "sort_keys": sort_keys}
    print(json.dumps(value, default=str, **options))


# Help for arguments, by command path; the "" entry applies to every command.
ARGUMENT_HELP: dict[str, dict[str, str]] = {
    "": {
        "--dir": "Directory that holds the workflow (default: the current directory)",
        "--config": "Workflow file, relative to --dir (default: outcome.yml)",
        "--agent": "Run every step with this agent instead of the workflow's",
        "--model": "Model for --agent",
        "--value": "Secret value; prefer --value-stdin, which keeps it out of shell history",
        "--value-stdin": "Read the secret value from stdin",
        "--workflow-id": "Grant this workflow ID access to the entry (repeatable)",
        "--step": "Step whose grants the call runs under",
        "--team": "Slack workspace, when the app is installed in several",
        "entry_id": "Vault entry ID, as oci vault list prints it",
        "capability": "Capability name, such as github.read",
    },
    "auth login": {
        "--api-url": "OutcomeCI API to sign in to",
        "--no-open": "Print the sign-in link instead of opening a browser",
    },
    "workflow get": {"workflow_id": "Cloud workflow ID"},
    "workflow sync": {
        "file": "Workflow file to upload with its .outcomeci/ support files",
        "--name": "Display name (default: the workflow's name)",
        "--create": "Create a new cloud workflow",
        "--version": "Add a version to the existing workflow with this name",
    },
    "workflow prepare-publication": {
        "file": "Workflow file to package for public reuse",
        "--output": "Directory to write the sanitized package to",
        "--agent": "Agent that reviews the package for private details",
        "--sensitive-term": "Term that must not appear in the package (repeatable)",
    },
    "vault put": {
        "path": "Vault path the workflow references as vault:<path>",
        "--name": "Display name (default: the path)",
    },
    "vault local put": {"path": "Vault path the workflow references as vault:<path>"},
    "integration execute": {
        "--input": "Call input as a JSON object",
        "--input-stdin": "Read the call input as JSON from stdin",
    },
    "integration slack setup": {"--name": "Slack app name"},
    "integration slack sync-credentials": {
        "--local": "Copy the bot token into the local Vault",
        "--cloud": "Copy the bot token into the --workspace-id workspace's Vault",
        "--workspace-id": "Cloud workspace identifier (with --cloud)",
        "--vault-dir": "Directory whose local Vault receives the token (with --local)",
        "--path": "Vault path to store the token at (default: slack/bot-token)",
        "--workflow-id": "Grant this cloud workflow ID access to the token (repeatable)",
    },
    "integration slack manifest": {"--project": "Slack CLI project directory"},
    "mcp init": {
        "--agent": "Only set up this agent (repeatable; default: every installed agent)",
        "--name": "Name the agent lists the server under (default: outcomeci)",
        "--api-url": "OutcomeCI API whose MCP server to add (default: the one you signed in to)",
        "--dry-run": "Print the commands without running them",
    },
}


COMMAND_HELP = {
    "auth login": "Sign in to OutcomeCI Cloud",
    "auth status": "Show the signed-in account",
    "auth logout": "Remove stored OutcomeCI credentials",
    "workflow sync": "Upload a workflow to OutcomeCI Cloud as a new workflow or version",
    "vault list": "List a workspace's Vault entries, without values",
    "vault put": "Store a credential in a workspace's Vault",
    "vault rotate": "Replace a Vault entry's value",
    "vault grant": "Set which workflows may use a Vault entry",
    "vault revoke": "Revoke a Vault entry",
    "vault local init": "Create this checkout's encrypted local Vault",
    "vault local list": "List local Vault entries, without values",
    "vault local put": "Store a credential in the local Vault",
    "integration slack setup": "Create and install a Slack app with the Slack CLI",
    "integration slack status": "Check the Slack app and Slack CLI",
    "integration slack manifest": "Print the Slack app manifest",
    "integration slack sync-credentials": "Copy the Slack app's bot token into a Vault",
    "mcp init": "Add the OutcomeCI MCP server to Claude Code, Codex and OpenCode",
}


def _describe(parser: argparse.ArgumentParser, path: str = "") -> None:
    """Give every command and argument without its own help the description
    COMMAND_HELP or ARGUMENT_HELP holds."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                if choice.help is None:
                    choice.help = COMMAND_HELP.get(f"{path} {choice.dest}".strip())
            # Commands left out of the listing on purpose stay hidden.
            listed = {choice.dest: choice for choice in action._choices_actions}
            action._choices_actions[:] = [
                listed.get(name)
                or action._ChoicesPseudoAction(name, (), COMMAND_HELP[f"{path} {name}".strip()])
                for name in action.choices
                if name in listed or f"{path} {name}".strip() in COMMAND_HELP
            ]
            for name, child in action.choices.items():
                _describe(child, f"{path} {name}".strip())
            continue
        if action.help is not None or isinstance(action, argparse._HelpAction):
            continue
        for key in (*action.option_strings, action.dest):
            found = ARGUMENT_HELP.get(path, {}).get(key) or ARGUMENT_HELP[""].get(key)
            if found:
                action.help = found
                break


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="oci", description="Write, run and publish OutcomeCI workflows"
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
    workflow = commands.add_parser("workflow", help="Compile, run and publish workflows")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_compile = workflow_commands.add_parser(
        "compile", help="Print the compiled workflow: steps, grants, instructions and schemas"
    )
    workflow_compile.add_argument("--dir", type=Path, default=Path.cwd())
    workflow_compile.add_argument("--config", type=Path, default=Path("outcome.yml"))
    workflow_compile.add_argument("--step", help="Print only this step's instructions")
    workflow_get = workflow_commands.add_parser(
        "get", help="Fetch a workflow's latest cloud revision"
    )
    workflow_get.add_argument("workflow_id")
    workflow_get.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    workflow_get.add_argument(
        "--output", type=Path, help="Write the revision content to this file instead of stdout"
    )
    workflow_sync = workflow_commands.add_parser("sync")
    workflow_publish = workflow_commands.add_parser(
        "prepare-publication",
        help="Create and verify a sanitized package for public reuse",
    )
    workflow_publish.add_argument("file", type=Path)
    workflow_publish.add_argument("--output", type=Path, required=True)
    workflow_publish.add_argument(
        "--agent", choices=("codex", "claude", "opencode"), default="codex"
    )
    workflow_publish.add_argument("--model")
    workflow_publish.add_argument("--sensitive-term", action="append", default=[])
    run_command = workflow_commands.add_parser(
        "run",
        help="Run a workflow in the runner container, with the local Vault and this "
        "machine's agent login, or with --cloud the workspace's",
    )
    run_command.add_argument("--dir", type=Path, default=Path.cwd())
    run_command.add_argument("--config", type=Path, default=Path("outcome.yml"))
    run_command.add_argument(
        "--trigger", help="Trigger to run; defaults to the only trigger, or the manual one"
    )
    run_command.add_argument("--payload", type=Path, help="JSON file to use as the trigger payload")
    run_command.add_argument("--agent", choices=AGENT_CHOICES)
    run_command.add_argument("--model")
    run_command.add_argument(
        "--auto-continue",
        action="store_true",
        help="Continue automatically into each ready step, including any real side "
        "effects (e.g. sending Slack messages) later steps perform",
    )
    run_command.add_argument(
        "--image", help="Runner image; defaults to the one released with this CLI"
    )
    run_command.add_argument(
        "--retry",
        metavar="RUN_ID",
        help="Resume a run in --dir that stopped on an error, from its recorded state",
    )
    run_command.add_argument(
        "--network",
        help="Docker network for the container, such as host when the default bridge "
        "network cannot resolve DNS",
    )
    run_command.add_argument(
        "--cloud",
        action="store_true",
        help="Use the cloud workflow's Vault grants and the workspace's connected agent",
    )
    run_command.add_argument("--workspace-id", help="Cloud workspace identifier (with --cloud)")
    run_command.add_argument("--workflow-id", help="Cloud workflow identifier (with --cloud)")
    workflow_sync.add_argument("file", type=Path)
    workflow_sync.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    workflow_sync.add_argument("--name")
    workflow_mode = workflow_sync.add_mutually_exclusive_group(required=True)
    workflow_mode.add_argument("--create", action="store_true")
    workflow_mode.add_argument("--version", action="store_true")
    dataset = commands.add_parser("dataset", help="Check and export the records every run stores")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_export = dataset_commands.add_parser(
        "export",
        help="Walk run directories, report what each holds, and write one JSON line per run",
    )
    dataset_export.add_argument(
        "--workspace-id", help="Cloud workspace whose stored runs to export"
    )
    dataset_export.add_argument(
        "--workflow-id", help="Only this workflow's runs (with --workspace-id)"
    )
    _add_dir_argument(dataset_export)
    dataset_export.add_argument(
        "--out", help="JSON lines file to write (default: no rows, report only)"
    )
    dataset_export.add_argument(
        "--check", action="store_true", help="Report each run's completeness without writing rows"
    )
    vault = commands.add_parser(
        "vault", help="Manage workspace credentials through OutcomeCI Vault"
    )
    vault_commands = vault.add_subparsers(dest="vault_command", required=True)
    vault_list = vault_commands.add_parser("list")
    vault_list.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    vault_put = vault_commands.add_parser("put")
    vault_put.add_argument("path")
    vault_put.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    vault_put.add_argument("--name")
    vault_put.add_argument("--value")
    vault_put.add_argument("--value-stdin", action="store_true")
    vault_put.add_argument("--workflow-id", action="append", default=[])
    vault_put.add_argument("--provider", help="Credential provider, for example slack")
    credentials.add_arguments(vault_put)
    vault_rotate = vault_commands.add_parser("rotate")
    vault_rotate.add_argument("entry_id")
    vault_rotate.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    vault_rotate.add_argument("--value")
    vault_rotate.add_argument("--value-stdin", action="store_true")
    vault_grant = vault_commands.add_parser("grant")
    vault_grant.add_argument("entry_id")
    vault_grant.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    vault_grant.add_argument("--workflow-id", action="append", default=[])
    vault_revoke = vault_commands.add_parser("revoke")
    vault_revoke.add_argument("entry_id")
    vault_revoke.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    local_vault = vault_commands.add_parser("local", help="Manage an encrypted offline Vault")
    local_vault_commands = local_vault.add_subparsers(dest="local_vault_command", required=True)
    local_vault_init = local_vault_commands.add_parser("init")
    _add_dir_argument(local_vault_init)
    local_vault_list = local_vault_commands.add_parser("list")
    _add_dir_argument(local_vault_list)
    local_vault_put = local_vault_commands.add_parser("put")
    local_vault_put.add_argument("path")
    local_vault_put.add_argument("--value")
    local_vault_put.add_argument("--value-stdin", action="store_true")
    credentials.add_arguments(local_vault_put)
    _add_dir_argument(local_vault_put)
    mcp = commands.add_parser("mcp", help="Connect your coding agents to OutcomeCI over MCP")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_init = mcp_commands.add_parser("init")
    mcp_init.add_argument("--agent", action="append", choices=mcp_setup.AGENT_IDS, default=[])
    mcp_init.add_argument("--name", default=mcp_setup.DEFAULT_NAME)
    mcp_init.add_argument("--api-url")
    mcp_init.add_argument("--dry-run", action="store_true")
    init = commands.add_parser("init", help="Write a starter workflow into a directory")
    init.add_argument("--dir", type=Path, default=Path.cwd())
    validate_command = commands.add_parser(
        "validate", help="Compile the workflow and print its revision"
    )
    validate_command.add_argument("--dir", type=Path, default=Path.cwd())
    validate_command.add_argument("--config", type=Path, default=Path("outcome.yml"))
    integration = commands.add_parser("integration", help="Set up a provider's app, such as Slack")
    # list, describe, execute, dry-run and doctor are how a running step's agent
    # calls its granted APIs; they stay out of help and usage.
    integration_commands = integration.add_subparsers(
        dest="integration_command", required=True, metavar="{slack}"
    )
    integration_list = integration_commands.add_parser("list")
    integration_list.add_argument("--step")
    _add_workflow_arguments(integration_list)
    integration_describe = integration_commands.add_parser("describe")
    integration_describe.add_argument("capability")
    _add_workflow_arguments(integration_describe)
    integration_execute = integration_commands.add_parser("execute")
    integration_execute.add_argument("capability")
    integration_execute.add_argument("--step", required=True)
    integration_execute.add_argument("--input", default="{}")
    integration_execute.add_argument("--input-stdin", action="store_true")
    _add_workflow_arguments(integration_execute)
    integration_dry_run = integration_commands.add_parser("dry-run")
    integration_dry_run.add_argument("--step", required=True)
    _add_workflow_arguments(integration_dry_run)
    integration_doctor = integration_commands.add_parser("doctor")
    integration_doctor.add_argument(
        "--connectivity", action="store_true", help="Also check configured HTTP origins"
    )
    _add_workflow_arguments(integration_doctor)
    slack = integration_commands.add_parser(
        "slack", help="Create, inspect and connect a Slack app for a workflow"
    )
    slack_commands = slack.add_subparsers(dest="slack_command", required=True)
    slack_setup = slack_commands.add_parser("setup")
    _add_dir_argument(slack_setup)
    slack_setup.add_argument("--name", default="OutcomeCI")
    slack_setup.add_argument("--team")
    slack_setup.add_argument(
        "--request-url",
        help=(
            "The workflow's webhook URL, which Slack sends events to. Leave it out "
            "on the first run; add it once the signing secret is in the Vault"
        ),
    )
    slack_setup.add_argument(
        "--event",
        action="append",
        choices=["mention", "dm"],
        help="An event the workflow's trigger listens for (repeatable; default: both)",
    )
    slack_sync = slack_commands.add_parser(
        "sync-credentials", help="Sync the installed app token to a Vault"
    )
    _add_dir_argument(slack_sync)
    slack_destination = slack_sync.add_mutually_exclusive_group(required=True)
    slack_destination.add_argument("--local", action="store_true")
    slack_destination.add_argument("--cloud", action="store_true")
    slack_sync.add_argument("--workspace-id")
    slack_sync.add_argument("--vault-dir", type=Path)
    slack_sync.add_argument("--team")
    slack_sync.add_argument("--path", default="slack/bot-token")
    slack_sync.add_argument("--workflow-id", action="append")
    slack_status_command = slack_commands.add_parser("status")
    _add_dir_argument(slack_status_command)
    slack_manifest_command = slack_commands.add_parser(
        "manifest", help="Print the generated Slack app manifest"
    )
    slack_manifest_command.add_argument("--project", type=Path, default=Path.cwd())
    slack_manifest_command.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    _describe(root)
    return root


def _mcp_init(args: argparse.Namespace) -> int:
    """Add the hosted MCP server to every installed agent, or to --agent only."""
    api_url = args.api_url or mcp_setup.default_api_url(cloud_credentials_path())
    result = mcp_setup.init(
        api_url=api_url,
        name=args.name,
        agents=args.agent,
        dry_run=args.dry_run,
        login=sys.stdin.isatty(),
        report=lambda line: print(line, file=sys.stderr),
    )
    statuses = [entry["status"] for entry in result["agents"]]
    if all(status == "not_installed" for status in statuses):
        names = ", ".join(agent.executable for agent in mcp_setup.AGENTS)
        raise ExecutionError(
            f"No supported agent found on PATH ({names}). "
            f"Add the server by URL in your agent instead: {result['url']}"
        )
    _print_json(result)
    failed = "failed" in statuses or (bool(args.agent) and "not_installed" in statuses)
    return 1 if failed else 0


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
            if args.workflow_command == "compile":
                result = compile_workflow((args.dir / args.config).resolve())
                if args.step:
                    step = result["instructions"]["steps"].get(args.step)
                    if step is None:
                        raise ExecutionError(f"workflow has no step {args.step}")
                    result = {
                        **result,
                        "instructions": {
                            "orchestrator": result["instructions"]["orchestrator"],
                            "step": step,
                        },
                    }
                _print_json(result, sort_keys=True)
                return 0
            if args.workflow_command == "get":
                result = get_workflow(args.workspace_id, args.workflow_id)
                if args.output:
                    args.output.write_text(result["content"], encoding="utf-8")
                    support_files = []
                    for relative, encoded in sorted(result.get("files", {}).items()):
                        if not relative.startswith(".outcomeci/") or ".." in Path(relative).parts:
                            raise ExecutionError(
                                f"workflow support file path is invalid: {relative}"
                            )
                        support_path = args.output.parent / relative
                        support_path.parent.mkdir(parents=True, exist_ok=True)
                        support_path.write_bytes(base64.b64decode(encoded))
                        support_files.append(str(support_path))
                    _print_json(
                        {
                            "workflow_id": result["workflow_id"],
                            "revision": result["revision"],
                            "content_sha256": result["content_sha256"],
                            "written_to": str(args.output),
                            "support_files_written": support_files,
                        }
                    )
                else:
                    _print_json(result)
                return 0
            if args.workflow_command == "prepare-publication":
                result = prepare_publication(
                    args.file,
                    args.output,
                    agent=args.agent,
                    model=args.model,
                    sensitive_terms=args.sensitive_term,
                )
                _print_json(result, sort_keys=True)
                return 0
            if args.workflow_command == "run":
                root = args.dir.resolve()
                if args.cloud:
                    if not args.workspace_id or not args.workflow_id:
                        raise ExecutionError("--cloud needs --workspace-id and --workflow-id")
                    _print_json(
                        workflow_run.run_cloud(
                            root,
                            root / args.config,
                            args.workspace_id,
                            args.workflow_id,
                            trigger_name=args.trigger,
                            payload_path=args.payload,
                            agent=args.agent,
                            model=args.model,
                            auto_continue=args.auto_continue,
                            image=args.image,
                            network=args.network,
                            retry_run=args.retry,
                        )
                    )
                    return 0
                if args.workspace_id or args.workflow_id:
                    raise ExecutionError("--workspace-id and --workflow-id need --cloud")
                _print_json(
                    workflow_run.run_local(
                        root,
                        root / args.config,
                        trigger_name=args.trigger,
                        payload_path=args.payload,
                        agent=args.agent,
                        model=args.model,
                        auto_continue=args.auto_continue,
                        image=args.image,
                        network=args.network,
                        retry_run=args.retry,
                    )
                )
                return 0
            result = sync_workflow(
                args.file,
                args.workspace_id,
                args.name,
                "create" if args.create else "version",
            )
            _print_json(result)
            return 0
        if args.command == "dataset":
            from . import dataset as dataset_module

            rows: list[dict[str, object]] = []
            check_only = args.check or not args.out
            if args.workspace_id:
                report = dataset_module.workspace_export(
                    args.workspace_id,
                    workflow_id=args.workflow_id,
                    check_only=check_only,
                    emit=rows.append,
                )
            else:
                report = dataset_module.local_export(
                    args.dir.resolve(), check_only=check_only, emit=rows.append
                )
            if args.out and not args.check:
                with open(args.out, "w", encoding="utf-8") as stream:
                    for row in rows:
                        stream.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
                report["out"] = args.out
                report["rows"] = len(rows)
            _print_json(report)
            return 0 if report["incomplete"] == 0 else 1
        if args.command == "vault":
            if args.vault_command == "local":
                workspace = args.dir.resolve()
                if args.local_vault_command == "init":
                    _print_json(initialize_local_vault(workspace))
                elif args.local_vault_command == "list":
                    _print_json(list_local_vault_entries(workspace), sort_keys=True)
                else:
                    if args.secrets_json_stdin and not args.credential_type:
                        raise ExecutionError("--secrets-json-stdin needs --credential-type")
                    value, secrets = _secret_input(args, prompt=True)
                    if args.credential_type:
                        value = json.dumps(
                            credentials.build(
                                args.credential_type, args, value=value, secrets=secrets
                            )
                        )
                    _print_json(put_local_vault_entry(workspace, args.path, value))
                return 0
            if args.vault_command == "list":
                result = vault_request(args.workspace_id, "list")
            elif args.vault_command == "put":
                typed = bool(args.provider or args.credential_type)
                if typed and not (args.provider and args.credential_type):
                    raise ExecutionError(
                        "--provider and --credential-type must be provided together"
                    )
                if args.secrets_json_stdin and not typed:
                    raise ExecutionError("--secrets-json-stdin needs --credential-type")
                value, secrets = _secret_input(args, prompt=False)
                if typed and args.value is not None:
                    raise ExecutionError("typed credentials must be supplied through stdin")
                if typed:
                    result = vault_request(
                        args.workspace_id,
                        "put_credential",
                        path=args.path,
                        display_name=args.name or args.path,
                        provider=args.provider,
                        credential=credentials.build(
                            args.credential_type, args, value=value, secrets=secrets
                        ),
                        workflow_ids=args.workflow_id,
                    )
                else:
                    result = vault_request(
                        args.workspace_id,
                        "put",
                        path=args.path,
                        display_name=args.name or args.path,
                        value=value,
                        workflow_ids=args.workflow_id,
                    )
            elif args.vault_command == "rotate":
                value = sys.stdin.read().rstrip("\n") if args.value_stdin else args.value
                if not value:
                    raise ExecutionError("secret value is required; use --value-stdin or --value")
                result = vault_request(
                    args.workspace_id, "rotate", entry_id=args.entry_id, value=value
                )
            elif args.vault_command == "grant":
                result = vault_request(
                    args.workspace_id,
                    "grant",
                    entry_id=args.entry_id,
                    workflow_ids=args.workflow_id,
                )
            else:
                result = vault_request(args.workspace_id, "revoke", entry_id=args.entry_id)
            _print_json(result or {"ok": True})
            return 0
        if args.command == "mcp":
            return _mcp_init(args)
        if args.command == "init":
            _print_json({"created": initialize(args.dir)})
        elif args.command == "validate":
            result = compile_workflow((args.dir / args.config).resolve())
            _print_json(
                {
                    "valid": True,
                    "workflow_revision": result["workflow_revision"],
                    "model_capabilities": result["model_capabilities"],
                    "capability_warnings": result["capability_warnings"],
                }
            )
        elif args.command == "integration" and args.integration_command in {
            "list",
            "describe",
            "execute",
            "dry-run",
            "doctor",
        }:
            compiled = compile_workflow(_workflow_path(args))
            executor = IntegrationExecutor(
                compiled, resolver=local_credential_resolver(_workflow_path(args).parent)
            )
            if args.integration_command == "list":
                _print_json({"capabilities": executor.capabilities(args.step)}, sort_keys=True)
            elif args.integration_command == "describe":
                _print_json(executor.describe(args.capability), sort_keys=True)
            elif args.integration_command == "dry-run":
                _print_json(executor.dry_run(args.step), sort_keys=True)
            elif args.integration_command == "doctor":
                result = doctor(
                    compiled,
                    connectivity=args.connectivity,
                    resolver=local_credential_resolver(_workflow_path(args).parent),
                )
                _print_json(result, sort_keys=True)
                return 0 if result["ok"] else 2
            else:
                raw = sys.stdin.read() if args.input_stdin else args.input
                try:
                    inputs = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ExecutionError("integration input must be valid JSON") from exc
                if not isinstance(inputs, dict):
                    raise ExecutionError("integration input must be a JSON object")
                result = (
                    invoke_integration(args.capability, inputs)
                    if os.environ.get("OUTCOMECI_CAPABILITY_SOCKET")
                    else executor.execute(args.capability, inputs, step=args.step)
                )
                _print_json(result, sort_keys=True)
        elif args.command == "integration" and args.integration_command == "slack":
            if args.slack_command == "setup":
                _print_json(
                    setup_slack(
                        args.dir,
                        name=args.name,
                        request_url=args.request_url,
                        events=args.event or ["mention", "dm"],
                        team=args.team,
                    ),
                    sort_keys=True,
                )
            elif args.slack_command == "sync-credentials":
                if args.workspace_id and not args.cloud:
                    raise SlackError("--workspace-id is only valid with --cloud")
                _print_json(
                    slack_vault.sync_credentials(
                        args.dir,
                        local=args.local,
                        cloud_workspace=args.workspace_id if args.cloud else None,
                        vault_workspace=args.vault_dir,
                        team=args.team,
                        path=args.path,
                        workflows=args.workflow_id,
                    )
                )
            elif args.slack_command == "status":
                _print_json(slack_status(args.dir), sort_keys=True)
            elif args.slack_command == "manifest":
                _print_json(slack_manifest(args.source or args.project), compact=True)
        return 0
    except IntegrationError as exc:
        print(json.dumps({"error": exc.as_dict()}, sort_keys=True), file=sys.stderr)
        return 1 if exc.retryable else 2
    except (ConfigError, ExecutionError, SlackError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, ExecutionError) and exc.retryable else 2
    except KeyboardInterrupt:
        # Ctrl-C has already stopped whatever was running; a traceback here
        # would only bury that. 130 is the shell's exit status for SIGINT.
        print("oci: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
