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

from . import __version__, debug, slack_vault
from .capability import invoke_integration
from .cloud import auth_status as cloud_auth_status
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
from .repository import RepositoryError, initialize, validate

AGENT_CHOICES = ("codex", "claude")


def _add_workspace_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument("--workspace", type=Path, default=Path.cwd())


def _add_workflow_arguments(command: argparse.ArgumentParser) -> None:
    _add_workspace_argument(command)
    command.add_argument("--config", type=Path)


def _workflow_path(args: argparse.Namespace) -> Path:
    return (args.config or args.workspace / "outcome.yml").resolve()


def _print_json(value: object, *, compact: bool = False, sort_keys: bool = False) -> None:
    options = {"separators": (",", ":")} if compact else {"indent": 2, "sort_keys": sort_keys}
    print(json.dumps(value, default=str, **options))


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
    workflow_debug = workflow_commands.add_parser(
        "debug",
        help="Run a backend: outcomeci workflow locally against real cloud vault credentials",
    )
    workflow_debug.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    workflow_debug.add_argument("--workflow", required=True, help="Cloud workflow identifier")
    workflow_debug.add_argument("--dir", type=Path, default=Path.cwd())
    workflow_debug.add_argument("--config", type=Path, default=Path("outcome.yml"))
    workflow_debug.add_argument(
        "--trigger", help="Named trigger to synthesize a payload for; ignored with --run"
    )
    workflow_debug.add_argument(
        "--run", dest="invocation_id", help="Claim and replay a real queued invocation by id"
    )
    workflow_debug.add_argument(
        "--payload", type=Path, help="JSON file to use as the trigger payload"
    )
    workflow_debug.add_argument("--agent", choices=AGENT_CHOICES)
    workflow_debug.add_argument("--model")
    workflow_debug.add_argument(
        "--auto-continue",
        action="store_true",
        help="Continue automatically into each ready phase, including any real side "
        "effects (e.g. sending Slack messages) later phases perform",
    )
    workflow_debug.add_argument(
        "--image",
        help="Run inside this runner image, leasing the workspace's cloud agent credential "
        "(held exclusively for the run, as a cloud run holds it)",
    )
    workflow_debug.add_argument(
        "--retry",
        metavar="RUN_ID",
        help="Resume a run in --dir that stopped on an error, from its recorded state",
    )
    workflow_debug.add_argument(
        "--network",
        help="Docker network for the --image container, such as host when the default "
        "bridge network cannot resolve DNS",
    )
    workflow_run = workflow_commands.add_parser(
        "run",
        help="Run a workflow in the runner container with the local Vault and this "
        "machine's agent login",
    )
    workflow_run.add_argument("--dir", type=Path, default=Path.cwd())
    workflow_run.add_argument("--config", type=Path, default=Path("outcome.yml"))
    workflow_run.add_argument(
        "--trigger", help="Trigger to run; defaults to the only trigger, or the manual one"
    )
    workflow_run.add_argument(
        "--payload", type=Path, help="JSON file to use as the trigger payload"
    )
    workflow_run.add_argument("--agent", choices=AGENT_CHOICES)
    workflow_run.add_argument("--model")
    workflow_run.add_argument(
        "--auto-continue",
        action="store_true",
        help="Continue automatically into each ready step, including any real side "
        "effects (e.g. sending Slack messages) later steps perform",
    )
    workflow_run.add_argument(
        "--image", help="Runner image; defaults to the one released with this CLI"
    )
    workflow_run.add_argument(
        "--retry",
        metavar="RUN_ID",
        help="Resume a run in --dir that stopped on an error, from its recorded state",
    )
    workflow_run.add_argument(
        "--network",
        help="Docker network for the container, such as host when the default bridge "
        "network cannot resolve DNS",
    )
    workflow_sync.add_argument("file", type=Path)
    workflow_sync.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    workflow_sync.add_argument("--name")
    workflow_mode = workflow_sync.add_mutually_exclusive_group(required=True)
    workflow_mode.add_argument("--create", action="store_true")
    workflow_mode.add_argument("--version", action="store_true")
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
    vault_rotate.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    vault_rotate.add_argument("--value")
    vault_rotate.add_argument("--value-stdin", action="store_true")
    vault_grant = vault_commands.add_parser("grant")
    vault_grant.add_argument("entry_id")
    vault_grant.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
    vault_grant.add_argument("--workflow", action="append", default=[])
    vault_revoke = vault_commands.add_parser("revoke")
    vault_revoke.add_argument("entry_id")
    vault_revoke.add_argument("--workspace-id", required=True, help="Cloud workspace identifier")
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
    init = commands.add_parser("init", help="Write a starter workflow into a directory")
    init.add_argument("--dir", type=Path, default=Path.cwd())
    validate_command = commands.add_parser(
        "validate", help="Compile the workflow and print its revision"
    )
    validate_command.add_argument("--dir", type=Path, default=Path.cwd())
    validate_command.add_argument("--config", type=Path, default=Path("outcome.yml"))
    status_command = commands.add_parser("status")
    status_command.add_argument("--dir", type=Path, default=Path.cwd())
    integration = commands.add_parser("integration", help="Set up a provider's app, such as Slack")
    # list, describe, execute, dry-run and doctor are how a running step's agent
    # calls its granted APIs; they stay out of help and usage.
    integration_commands = integration.add_subparsers(
        dest="integration_command", required=True, metavar="{slack}"
    )
    integration_list = integration_commands.add_parser("list")
    integration_list.add_argument("--phase")
    _add_workflow_arguments(integration_list)
    integration_describe = integration_commands.add_parser("describe")
    integration_describe.add_argument("capability")
    _add_workflow_arguments(integration_describe)
    integration_execute = integration_commands.add_parser("execute")
    integration_execute.add_argument("capability")
    integration_execute.add_argument("--phase", required=True)
    integration_execute.add_argument("--input", default="{}")
    integration_execute.add_argument("--input-stdin", action="store_true")
    _add_workflow_arguments(integration_execute)
    integration_dry_run = integration_commands.add_parser("dry-run")
    integration_dry_run.add_argument("--phase", required=True)
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
    _add_workspace_argument(slack_setup)
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
            if args.workflow_command == "compile":
                result = compile_workflow((args.dir / args.config).resolve())
                if args.step:
                    phase = result["instructions"]["phases"].get(args.step)
                    if phase is None:
                        raise ExecutionError(f"workflow has no step {args.step}")
                    result = {
                        **result,
                        "instructions": {
                            "orchestrator": result["instructions"]["orchestrator"],
                            "phase": phase,
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
                _print_json(
                    debug.run_local(
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
            if args.workflow_command == "debug":
                root = args.dir.resolve()
                config = (root / args.config).resolve()
                result = debug.run(
                    root,
                    config,
                    args.workspace_id,
                    args.workflow,
                    trigger_name=args.trigger,
                    invocation_id=args.invocation_id,
                    payload_path=args.payload,
                    agent=args.agent,
                    model=args.model,
                    auto_continue=args.auto_continue,
                    image=args.image,
                    network=args.network,
                    retry_run=args.retry,
                )
                _print_json(result)
                return 0
            result = sync_workflow(
                args.file,
                args.workspace_id,
                args.name,
                "create" if args.create else "version",
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
                        sys.stdin.read().rstrip("\n")
                        if args.value_stdin
                        else args.value
                        if args.value is not None
                        else getpass.getpass("Secret value: ")
                    )
                    _print_json(put_local_vault_entry(workspace, args.path, value))
                return 0
            if args.vault_command == "list":
                result = vault_request(args.workspace_id, "list")
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
                        args.workspace_id,
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
                        args.workspace_id, "rotate", entry_id=args.entry_id, value=value
                    )
            elif args.vault_command == "grant":
                result = vault_request(
                    args.workspace_id, "grant", entry_id=args.entry_id, workflow_ids=args.workflow
                )
            else:
                result = vault_request(args.workspace_id, "revoke", entry_id=args.entry_id)
            _print_json(result or {"ok": True})
            return 0
        if args.command == "init":
            _print_json({"created": initialize(args.dir)})
        elif args.command == "validate":
            result = compile_workflow((args.dir / args.config).resolve())
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
        elif args.command == "integration" and args.integration_command == "slack":
            if args.slack_command == "setup":
                _print_json(
                    setup_slack(
                        args.workspace,
                        name=args.name,
                        request_url=args.request_url,
                        events=args.event or ["mention", "dm"],
                        team=args.team,
                    ),
                    sort_keys=True,
                )
            elif args.slack_command == "sync-credentials":
                _print_json(
                    slack_vault.sync_credentials(
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
            elif args.slack_command == "manifest":
                _print_json(slack_manifest(args.source or args.project), compact=True)
        return 0
    except IntegrationError as exc:
        print(json.dumps({"error": exc.as_dict()}, sort_keys=True), file=sys.stderr)
        return 1 if exc.retryable else 2
    except (ConfigError, RepositoryError, ExecutionError, SlackError) as exc:
        print(f"oci: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, ExecutionError) and exc.retryable else 2


if __name__ == "__main__":
    raise SystemExit(main())
