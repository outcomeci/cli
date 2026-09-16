"""OutcomeCI command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from . import __version__
from .capability import invoke as invoke_capability
from .capability import invoke_integration
from .cloud import auth_status as cloud_auth_status
from .cloud import login as cloud_login
from .cloud import login_with_key as cloud_login_with_key
from .cloud import logout as cloud_logout
from .cloud import sync_workflow, vault_request
from .config import ConfigError, compile_workflow
from .conformance import run as run_conformance
from .contracts import ContractError, render_reference, validate_contract
from .humans import accept as accept_human_input
from .humans import assign as assign_human_hook
from .humans import poll as poll_human_input
from .humans import request as request_human_input
from .integrations import (
    IntegrationError,
    IntegrationExecutor,
    doctor,
    import_openapi,
    local_credential_resolver,
)
from .integrations import apply_patch as apply_integration_patch
from .integrations import propose_patch as propose_integration_patch
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
from .local import trigger as trigger_local_outcome
from .local_vault import initialize as initialize_local_vault
from .local_vault import list_entries as list_local_vault_entries
from .local_vault import put as put_local_vault_entry
from .locking import verify_lock, write_lock
from .mcp_server import serve as serve_mcp
from .outcome import run as run_outcome
from .process import ExecutionError
from .repository import RepositoryError, initialize, update, validate
from .schema import export_schema, load_schema, schema_path
from .simulation import bundled_definition
from .simulation import run as run_simulation
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
    tunnel = commands.add_parser("tunnel", help="Expose an approved local HTTP target")
    tunnel_commands = tunnel.add_subparsers(dest="tunnel_command", required=True)
    for name in ("start", "status", "stop"):
        action = tunnel_commands.add_parser(name)
        action.add_argument("--workspace", required=True)
        if name == "start":
            action.add_argument("--target", required=True)
            action.add_argument("--ttl-seconds", type=int, default=900)
            action.add_argument("--public", action="store_true", help="Acknowledge public exposure")
    schema = commands.add_parser("schema", help="Inspect the versioned outcome.yml schema")
    schema_commands = schema.add_subparsers(dest="schema_command", required=True)
    schema_path_command = schema_commands.add_parser("path", help="Print the packaged schema path")
    schema_print_command = schema_commands.add_parser("print", help="Print the current schema")
    schema_export = schema_commands.add_parser("export", help="Export the current schema")
    for command in (schema_path_command, schema_print_command, schema_export):
        command.add_argument(
            "--type",
            choices=("workflow", "email.received", "webhook.received", "agent"),
            default="workflow",
        )
    schema_commands.add_parser("docs", help="Print reference documentation from enforced schemas")
    schema_validate_command = schema_commands.add_parser(
        "validate", help="Validate a typed payload or phase configuration"
    )
    schema_validate_command.add_argument("input", type=Path)
    schema_validate_command.add_argument(
        "--type", choices=("email.received", "webhook.received", "agent"), required=True
    )
    schema_export.add_argument("output", type=Path)
    conformance = commands.add_parser(
        "conformance", help="Run the portable OutcomeCI runtime contract checks"
    )
    conformance.add_argument("--workflow", type=Path)
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
    workflow_listen = workflow_commands.add_parser(
        "listen", help="Execute queued workflow triggers locally"
    )
    workflow_listen.add_argument("--workspace", required=True, help="Cloud workspace identifier")
    workflow_listen.add_argument("--workflow", required=True, help="Cloud workflow identifier")
    workflow_listen.add_argument("--dir", type=Path, default=Path.cwd())
    workflow_listen.add_argument("--config", type=Path, default=Path("outcome.yml"))
    workflow_listen.add_argument(
        "--auto-continue",
        action="store_true",
        help="Continue configured phases automatically, without bypassing human hooks",
    )
    workflow_listen.add_argument("--once", action="store_true")
    workflow_sync.add_argument("file", type=Path)
    workflow_sync.add_argument("--workspace", required=True)
    workflow_sync.add_argument("--name")
    workflow_sync.add_argument(
        "--patch", type=Path, help="OutcomeWorkflowPatch that produced this version"
    )
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
    vault_put.add_argument(
        "--secrets-json-stdin",
        action="store_true",
        help="Read a JSON object of secret fields from stdin",
    )
    vault_put.add_argument("--workflow", action="append", default=[])
    vault_put.add_argument("--provider", help="Credential provider, for example slack")
    vault_put.add_argument(
        "--credential-type",
        choices=("api_key", "auth_header", "oauth2", "oidc"),
        help="Typed credential contract used by workflow brokers",
    )
    vault_put.add_argument("--header-name")
    vault_put.add_argument("--prefix")
    vault_put.add_argument("--scheme")
    vault_put.add_argument("--token-url")
    vault_put.add_argument("--issuer-url")
    vault_put.add_argument("--client-id")
    vault_put.add_argument("--grant-type", choices=("client_credentials", "refresh_token"))
    vault_put.add_argument("--scope", action="append", default=[])
    vault_put.add_argument("--audience")
    vault_put.add_argument(
        "--secret-name",
        choices=("api_key", "value", "client_secret", "refresh_token"),
        help="Field receiving --value/--value-stdin; inferred for common types",
    )
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
    local_vault = vault_commands.add_parser("local", help="Manage an encrypted offline Vault")
    local_vault_commands = local_vault.add_subparsers(dest="local_vault_command", required=True)
    local_vault_init = local_vault_commands.add_parser("init")
    _add_workspace_argument(local_vault_init)
    local_vault_list = local_vault_commands.add_parser("list")
    _add_workspace_argument(local_vault_list)
    local_vault_put = local_vault_commands.add_parser("put")
    local_vault_put.add_argument("path")
    local_vault_put.add_argument("--value")
    local_vault_put.add_argument("--value-stdin", action="store_true")
    _add_workspace_argument(local_vault_put)
    for name in ("init", "update", "validate", "status"):
        item = commands.add_parser(name)
        item.add_argument("--dir", type=Path, default=Path.cwd())
        if name == "init":
            item.add_argument("--backend", choices=("outcomeci", "filesystem"), default="outcomeci")
    proof = commands.add_parser("proof", help="Run ecosystem persona durability proofs")
    proof_commands = proof.add_subparsers(dest="proof_command", required=True)
    proof_run = proof_commands.add_parser("run")
    proof_source = proof_run.add_mutually_exclusive_group()
    proof_source.add_argument("--definition", type=Path)
    proof_source.add_argument("--name", choices=("local-first-v1", "email-trigger-v1"))
    proof_run.add_argument("--workspace", type=Path, default=Path("/proof"))
    proof_run.add_argument("--report", type=Path)
    outcome = commands.add_parser("outcome")
    outcome_commands = outcome.add_subparsers(dest="outcome_command", required=True)
    for name in ("validate", "compile"):
        item = outcome_commands.add_parser(name)
        item.add_argument("config", nargs="?", type=Path, default=Path("outcome.yml"))
        if name == "compile":
            item.add_argument("--phase")
            item.add_argument("--run")
            item.add_argument("--workspace", type=Path, default=Path.cwd())
    lock_command = outcome_commands.add_parser("lock", help="Pin resolved workflow contracts")
    lock_command.add_argument("config", nargs="?", type=Path, default=Path("outcome.yml"))
    lock_command.add_argument("--output", type=Path, default=Path("outcome.lock"))
    verify_lock_command = outcome_commands.add_parser(
        "verify-lock", help="Verify outcome.lock against the current workflow"
    )
    verify_lock_command.add_argument("config", nargs="?", type=Path, default=Path("outcome.yml"))
    verify_lock_command.add_argument("--lock", type=Path, default=Path("outcome.lock"))
    run = outcome_commands.add_parser("run")
    run.add_argument("--claim", required=True, type=Path)
    run.add_argument("--workspace", type=Path, default=Path("/workspace"))
    start = outcome_commands.add_parser("start")
    start.add_argument("intent")
    _add_workflow_arguments(start, agent_overrides=True)
    trigger_command = outcome_commands.add_parser(
        "trigger", help="Execute a named typed trigger locally"
    )
    trigger_command.add_argument("trigger_name")
    trigger_command.add_argument("--input", type=Path, required=True)
    _add_workflow_arguments(trigger_command, agent_overrides=True)
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
    integration_list = integration_commands.add_parser(
        "list", help="List authorized API capabilities"
    )
    integration_list.add_argument("--phase")
    _add_workflow_arguments(integration_list)
    integration_describe = integration_commands.add_parser(
        "describe", help="Describe one API capability without exposing credentials"
    )
    integration_describe.add_argument("capability")
    _add_workflow_arguments(integration_describe)
    integration_execute = integration_commands.add_parser(
        "execute", help="Execute a workflow-authorized API capability"
    )
    integration_execute.add_argument("capability")
    integration_execute.add_argument("--phase", required=True)
    integration_execute.add_argument("--input", default="{}")
    integration_execute.add_argument("--input-stdin", action="store_true")
    _add_workflow_arguments(integration_execute)
    integration_dry_run = integration_commands.add_parser(
        "dry-run", help="Show authorized API and human effects without executing them"
    )
    integration_dry_run.add_argument("--phase", required=True)
    _add_workflow_arguments(integration_dry_run)
    integration_doctor = integration_commands.add_parser(
        "doctor", help="Diagnose integration configuration and credential references"
    )
    integration_doctor.add_argument(
        "--connectivity", action="store_true", help="Also check configured HTTP origins"
    )
    _add_workflow_arguments(integration_doctor)
    integration_mcp = integration_commands.add_parser(
        "mcp", help="Serve phase-authorized integrations as MCP tools over stdio"
    )
    integration_mcp.add_argument("--phase", required=True)
    _add_workflow_arguments(integration_mcp)
    integration_import = integration_commands.add_parser(
        "import-openapi", help="Propose declared operations from an OpenAPI allowlist"
    )
    integration_import.add_argument("integration")
    integration_import.add_argument("--output", type=Path, required=True)
    _add_workflow_arguments(integration_import)
    integration_patch = integration_commands.add_parser(
        "patch", help="Create or apply a version-producing workflow patch"
    )
    patch_commands = integration_patch.add_subparsers(dest="patch_command", required=True)
    patch_propose = patch_commands.add_parser("propose")
    patch_propose.add_argument("capability")
    patch_propose.add_argument("--definition", type=Path, required=True)
    patch_propose.add_argument("--reason", required=True)
    patch_propose.add_argument("--run", required=True)
    patch_propose.add_argument("--phase", required=True)
    patch_propose.add_argument("--agent", required=True)
    patch_propose.add_argument("--output", type=Path, required=True)
    _add_workflow_arguments(patch_propose)
    patch_apply = patch_commands.add_parser("apply")
    patch_apply.add_argument("patch", type=Path)
    patch_apply.add_argument("--output", type=Path, required=True)
    _add_workflow_arguments(patch_apply)
    slack = integration_commands.add_parser("slack")
    slack_commands = slack.add_subparsers(dest="slack_command", required=True)
    slack_setup = slack_commands.add_parser("setup")
    _add_workspace_argument(slack_setup)
    slack_setup.add_argument("--name", default="OutcomeCI")
    slack_setup.add_argument("--team")
    slack_setup.add_argument("--channel", help="Default Slack channel or user ID for human hooks")
    slack_setup.add_argument("--force", action="store_true")
    slack_sync = slack_commands.add_parser(
        "sync-credentials", help="Sync the installed app token to a Vault"
    )
    _add_workspace_argument(slack_sync)
    slack_destination = slack_sync.add_mutually_exclusive_group(required=True)
    slack_destination.add_argument("--local", action="store_true")
    slack_destination.add_argument("--cloud", metavar="WORKSPACE_ID")
    slack_sync.add_argument("--vault-workspace", type=Path)
    slack_sync.add_argument("--team")
    slack_sync.add_argument("--path", default="slack/bot-token")
    slack_sync.add_argument("--workflow", action="append")
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
        if args.command == "tunnel":
            from . import tunnels

            if args.tunnel_command == "start":
                tunnels.start(
                    args.workspace, args.target, ttl_seconds=args.ttl_seconds, public=args.public
                )
            else:
                _print_json(
                    tunnels.status(args.workspace)
                    if args.tunnel_command == "status"
                    else tunnels.stop(args.workspace)
                )
            return 0
        if args.command == "proof":
            definition = args.definition or (bundled_definition(args.name) if args.name else None)
            result = run_simulation(definition, args.workspace, args.report)
            _print_json(result, sort_keys=True)
            return 0 if result["status"] == "passed" else 2
        if args.command == "schema":
            if args.schema_command == "path":
                print(schema_path(args.type))
            elif args.schema_command == "print":
                _print_json(load_schema(args.type), sort_keys=True)
            elif args.schema_command == "docs":
                print(render_reference())
            elif args.schema_command == "validate":
                try:
                    validate_contract(args.type, json.loads(args.input.read_text(encoding="utf-8")))
                except (ContractError, OSError, json.JSONDecodeError) as exc:
                    raise ExecutionError(str(exc)) from exc
                _print_json({"valid": True, "type": args.type})
            else:
                print(export_schema(args.output, args.type))
            return 0
        if args.command == "conformance":
            result = run_conformance(args.workflow.resolve() if args.workflow else None)
            _print_json(result, sort_keys=True)
            return 0 if result["conformant"] else 2
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
            if args.workflow_command == "listen":
                from .webhooks import listen

                root = args.dir.resolve()
                config = (root / args.config).resolve()
                listen(
                    args.workspace,
                    args.workflow,
                    root,
                    config,
                    auto_continue=args.auto_continue,
                    once=args.once,
                )
                return 0
            result = sync_workflow(
                args.file,
                args.workspace,
                args.name,
                "create" if args.create else "version",
                patch_path=args.patch,
            )
            _print_json(result)
            return 0
        if args.command == "vault":
            if args.vault_command == "local":
                workspace = args.workspace.resolve()
                if args.local_vault_command == "init":
                    _print_json(initialize_local_vault(workspace))
                elif args.local_vault_command == "list":
                    _print_json(list_local_vault_entries(workspace), sort_keys=True)
                else:
                    value = (
                        sys.stdin.read()
                        if args.value_stdin
                        else args.value
                        if args.value is not None
                        else getpass.getpass("Secret value: ")
                    )
                    _print_json(put_local_vault_entry(workspace, args.path, value))
                return 0
            if args.vault_command == "list":
                result = vault_request(args.workspace, "list")
            elif args.vault_command in {"put", "rotate"}:
                reads_stdin = args.value_stdin or getattr(args, "secrets_json_stdin", False)
                value = sys.stdin.read().rstrip("\n") if reads_stdin else args.value
                if not value:
                    raise ExecutionError("secret value is required; use --value-stdin or --value")
                if args.vault_command == "put":
                    typed = bool(args.provider or args.credential_type)
                    if typed and not (args.provider and args.credential_type):
                        raise ExecutionError(
                            "--provider and --credential-type must be provided together"
                        )
                    if typed and not reads_stdin:
                        raise ExecutionError("typed credentials must be supplied through stdin")
                    secrets = None
                    if args.secrets_json_stdin:
                        try:
                            secrets = json.loads(value)
                        except json.JSONDecodeError as exc:
                            raise ExecutionError(
                                "--secrets-json-stdin requires a JSON object"
                            ) from exc
                        if not isinstance(secrets, dict) or not all(
                            isinstance(key, str) and isinstance(item, str)
                            for key, item in secrets.items()
                        ):
                            raise ExecutionError("--secrets-json-stdin requires string fields")
                    result = vault_request(
                        args.workspace,
                        "put_credential" if typed else "put",
                        path=args.path,
                        display_name=args.name or args.path,
                        value=value,
                        workflow_ids=args.workflow,
                        provider=args.provider,
                        credential_type=args.credential_type,
                        header_name=args.header_name,
                        prefix=args.prefix,
                        scheme=args.scheme,
                        token_url=args.token_url,
                        issuer_url=args.issuer_url,
                        client_id=args.client_id,
                        grant_type=args.grant_type,
                        scopes=args.scope,
                        audience=args.audience,
                        secret_name=args.secret_name,
                        secrets=secrets,
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
        elif args.command == "outcome" and args.outcome_command == "lock":
            result = write_lock(args.config.resolve(), args.output.resolve())
            _print_json(
                {
                    "lock": str(args.output.resolve()),
                    "workflow_revision": result["workflow_revision"],
                },
                sort_keys=True,
            )
        elif args.command == "outcome" and args.outcome_command == "verify-lock":
            _print_json(verify_lock(args.config.resolve(), args.lock.resolve()), sort_keys=True)
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
        elif args.command == "outcome" and args.outcome_command == "trigger":
            try:
                payload = json.loads(args.input.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExecutionError("trigger input must be a readable JSON file") from exc
            if not isinstance(payload, dict):
                raise ExecutionError("trigger input must be a JSON object")
            _print_json(
                trigger_local_outcome(
                    args.workspace.resolve(),
                    _workflow_path(args),
                    args.trigger_name,
                    payload,
                    agent=args.agent,
                    model=args.model,
                ),
                sort_keys=True,
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
        elif args.command == "integration" and args.integration_command in {
            "list",
            "describe",
            "execute",
            "dry-run",
            "doctor",
            "mcp",
        }:
            compiled = compile_workflow(_workflow_path(args))
            executor = IntegrationExecutor(
                compiled, resolver=local_credential_resolver(_workflow_path(args).parent)
            )
            if args.integration_command == "list":
                _print_json({"capabilities": executor.capabilities(args.phase)}, sort_keys=True)
            elif args.integration_command == "describe":
                _print_json(executor.describe(args.capability), sort_keys=True)
            elif args.integration_command == "dry-run":
                _print_json(executor.dry_run(args.phase), sort_keys=True)
            elif args.integration_command == "doctor":
                result = doctor(
                    compiled,
                    connectivity=args.connectivity,
                    resolver=local_credential_resolver(_workflow_path(args).parent),
                )
                _print_json(result, sort_keys=True)
                return 0 if result["ok"] else 2
            elif args.integration_command == "mcp":
                serve_mcp(executor, args.phase)
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
                    else executor.execute(args.capability, inputs, phase=args.phase)
                )
                _print_json(result, sort_keys=True)
        elif args.command == "integration" and args.integration_command == "import-openapi":
            result = import_openapi(_workflow_path(args), args.integration)
            args.output.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
            _print_json(
                {
                    "patch": str(args.output),
                    "parent_revision": result["metadata"]["parentRevision"],
                    "operations": sorted(result["spec"]["operations"]["add"]),
                },
                sort_keys=True,
            )
        elif args.command == "integration" and args.integration_command == "patch":
            config = _workflow_path(args)
            if args.patch_command == "propose":
                try:
                    definition = yaml.safe_load(args.definition.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError) as exc:
                    raise ExecutionError(f"could not read operation definition: {exc}") from exc
                if not isinstance(definition, dict):
                    raise ExecutionError("operation definition must be a YAML mapping")
                integration_name, separator, operation_name = args.capability.partition(".")
                if not separator:
                    raise ExecutionError("capability must be integration.operation")
                result = propose_integration_patch(
                    config,
                    integration_name,
                    operation_name,
                    definition,
                    reason=args.reason,
                    run=args.run,
                    phase=args.phase,
                    agent=args.agent,
                )
                args.output.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
                _print_json(
                    {
                        "patch": str(args.output),
                        "parent_revision": result["metadata"]["parentRevision"],
                    }
                )
            else:
                _print_json(
                    apply_integration_patch(config, args.patch, args.output), sort_keys=True
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
            elif args.slack_command == "sync-credentials":
                from .slack_vault import sync_credentials

                _print_json(
                    sync_credentials(
                        args.workspace,
                        local=args.local,
                        cloud_workspace=args.cloud,
                        vault_workspace=args.vault_workspace,
                        team=args.team,
                        path=args.path,
                        workflows=args.workflow,
                    )
                )
            elif args.slack_command == "status":
                _print_json(slack_status(args.workspace), sort_keys=True)
            elif args.slack_command == "targets":
                _print_json(slack_targets(args.workspace.resolve()), sort_keys=True)
            elif args.slack_command == "manifest":
                _print_json(slack_manifest(args.source or args.project), compact=True)
        return 0
    except IntegrationError as exc:
        print(json.dumps({"error": exc.as_dict()}, sort_keys=True), file=sys.stderr)
        return 1 if exc.retryable else 2
    except (ConfigError, RepositoryError, TwinError, ExecutionError, SlackError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, ExecutionError) and exc.retryable else 2


if __name__ == "__main__":
    raise SystemExit(main())
