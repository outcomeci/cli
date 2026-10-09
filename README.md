# OutcomeCI CLI

`oci` writes, runs and publishes OutcomeCI workflows. A workflow is one
`outcome.yml` file in the `outcomeci.workflow/v1` format: what starts a run,
the APIs its steps may call, and its agent, model and runtime-driven steps. You iterate
on it locally, running it in the same runner container OutcomeCI Cloud uses,
then sync it to OutcomeCI Cloud where triggers run it on managed runners.

The starter needs Python 3.11 or newer, `pipx`, a running Docker daemon, and a
local Codex login (`codex login`).

```console
pipx install outcomeci-cli
oci init
# Edit .outcomeci/request.json: replace your-org/your-repo and set your task.
oci vault local init
oci vault local put github                  # prompts for a GitHub token with repo read access
oci validate
oci workflow run --payload .outcomeci/request.json --auto-continue
```

The full guide is at <https://outcomeci.com/docs/outcomeci>.

## Write a workflow

`oci init` writes a starter `outcome.yml`, its step instructions under
`.outcomeci/instructions/`, and a sample request. A workflow declares:

- `trigger`: `manual`, `email`, `webhook` (optionally verified by a provider,
  such as signed Slack events), a `cron` schedule, or a parent dispatcher.
- `secrets`: Vault references, such as `github: vault:github`.
- `apis`: a connector bound to a secret, such as
  `github: {uses: github, auth: secrets.github}`. Connectors come from
  [`outcomeci-connectors`](https://github.com/outcomeci/connectors).
- `reasoning`: the default agent (`codex`, `claude` or `opencode`), a fallback,
  and named profiles a step picks with `using: <name>`. A profile with a
  `runner` is an agent; one with only a `model`, such as
  `light: {model: anthropic/claude-haiku-4-5}`, reasons with direct model calls
  and no workspace, its granted APIs offered as tools. `review` sets the policy
  reviewer's model. A model profile's `key: secrets.<name>` selects your
  provider key. Managed Cloud runs can use platform keys for OpenAI and
  Anthropic when `key` is omitted; other chat providers require an explicit
  key. Policy-review model profiles support OpenAI and Anthropic.
- `steps`: agent or model steps with grants (`can:`) and an optional `policy:`, `await`
  steps that wait for a human signal, and `converse` steps that discuss a plan
  in a thread. Workflows with `type: dispatcher` can also use `decision` steps
  for typed model decisions and `dispatch` steps to queue child workflows.
  Dispatching children requires managed Cloud execution.

`oci validate` compiles the workflow and prints its revision; `oci workflow
compile` prints the compiled steps, grants, instructions and schemas.

## Credentials

A workflow names a credential and never an auth method. The Vault entry
decides how a request is authenticated: a plain value is a token, and a typed
credential carries its kind. Each connector declares the kinds its API accepts,
and a credential of any other kind fails before a request is sent.

| Kind | Vault credential |
| --- | --- |
| token | a plain value, or `--credential-type auth_header` |
| api_key | `--credential-type api_key` |
| basic | `--credential-type basic` with `username` and `password` |
| oauth2 | `--credential-type oauth2` with a client id and secret, and a refresh token for `--grant-type refresh_token` |
| oidc | `--credential-type oidc` with a client id and secret, and `--issuer-url` when the issuer varies per account |
| jwt_bearer | `--credential-type jwt_bearer` with `--issuer` and a private key |
| app_installation | `--credential-type app_installation` with `--app-id`, `--installation-id` and a private key, such as a GitHub App |

The credential fields work for both the local Vault (`oci vault local put`) and
the workspace Vault (`oci vault put`). A typed workspace credential also requires
`--workspace-id ID` and `--provider PROVIDER` alongside `--credential-type`.
Pass secret fields on stdin: `--value-stdin` for one value, or
`--secrets-json-stdin` for a JSON object such as
`{"username": "...", "password": "..."}`.

Connector credentials stay with the broker. It checks calls against the step's
grants, resolves the credential and sends the request. Agent login credentials
are supplied separately to the chosen runner. A provider that rotates a refresh
token revokes the old one, so the runtime
saves the new one to the Vault the credential came from before it is used again.

### LinkedIn credentials

The bundled LinkedIn connector uses your own app's client ID, client secret,
and refresh token. Your app needs approval for the requested scopes and for
programmatic refresh tokens. Save the selected scopes with repeated `--scope`
options on an OAuth2 Vault credential using `--grant-type refresh_token`.
Pass the client secret and refresh token through `--secrets-json-stdin`.
The broker validates the selected scopes against the connector declaration and
uses only that subset when refreshing access. Scope choices work with both the
local and workspace Vault, without relying on the web UI.

This connector currently declares account authorization only; LinkedIn API
operations are not yet available. The CLI does not initiate browser consent.

## Run a workflow

`oci workflow run` runs the workflow in `--dir` (the current directory by default)
inside a local Docker container. This remains local execution with `--cloud`.
Released CLIs select the matching runner image; development builds need
`--image IMAGE`.

- With no cloud options, secrets come from the local Vault and the agent login
  from this machine: Codex from `$CODEX_HOME/auth.json` or `~/.codex/auth.json`, Claude from
  `CLAUDE_CODE_OAUTH_TOKEN` or the local Vault entry `agents/claude`, and
  OpenCode from `OPENROUTER_API_KEY` or `agents/opencode`.
- `--cloud --workspace-id ID --workflow-id ID` leases the workspace Vault's
  credentials and its connected agent instead.

`--payload FILE` supplies the trigger payload, `--auto-continue` advances through
ready steps (respecting conditions and waits), and `--retry RUN_ID` resumes a run
that stopped on an error. Model-only workflows do not need an agent login. Each run's
state lands in `.outcomeci/outcomes/<run>/`, and every API call and its outcome
in `.outcomeci/.broker/<run>/journal.json`.

### Human approval without changing the plan

Keep approval state outside the plan being reviewed. A conversation can return
`[plan, status, decision]`, where `decision` is `approved`, `rejected`, or
`undecided`. Gate side effects with `when: discuss.decision == "approved"`.
The plan itself should contain only the work being proposed, not a `decision`
field that changes when someone approves it. Use the separate decision directly
in downstream conditions; a later model step must not reinterpret it.

Explicit approval of the unchanged plan closes the discussion. Rejection
closes it with `decision: rejected`. A changed plan always requires a later
approval, even if the agent says it is approved in the same turn. Capped or
timed-out discussions return `undecided`. Existing `plan` and `status` outputs
remain supported; `status: converged` still means approval of the unchanged plan.

### Inspect and export run records

```console
oci dataset export --dir . --check
oci dataset export --dir . --out runs.jsonl
```

The first command reports which run records are present or missing; the second
writes one JSON line per run. Use `--workspace-id ID` to read stored Cloud runs,
optionally restricted with `--workflow-id ID`. The command exits with status 1
when any run is incomplete.

## Take it online

```console
oci auth login
oci vault put github --workspace-id ID --value-stdin
oci workflow sync outcome.yml --workspace-id ID --create
```

`oci workflow sync` compiles the workflow first, then uploads it with its
`.outcomeci/` support files. It never uploads the local Vault, the broker
journal or run outputs. Grant each Vault entry to the workflow with
`oci vault grant ENTRY_ID --workspace-id ID --workflow-id WORKFLOW_ID` (use the
entry ID returned by `oci vault put` or `oci vault list`). Use `--version` to add
a version to an existing workflow and
`oci workflow get` to fetch the latest cloud revision.

Cloud sign-in credentials are stored in `~/.config/outcomeci/credentials.json`,
or under `OUTCOMECI_CONFIG_HOME` when it is set. To choose another API, use
`oci auth login --api-url URL` or set `OUTCOMECI_API_URL` before signing in.
Subsequent authenticated commands use the API URL saved with that login.

## Connect your coding agent

```console
oci mcp init
```

`oci mcp init` adds the OutcomeCI MCP server to each coding agent it finds on
your `PATH`: Claude Code, Codex and OpenCode. It runs each agent's own
`mcp add` command and skips any agent that already reaches OutcomeCI, including
Claude Code with the OutcomeCI connector from claude.ai. Sign-in happens in your
browser through the agent: Codex and OpenCode open it straight away, and Claude
Code signs in from its `/mcp` menu. Use `--agent` to set up one agent and
`--dry-run` to see the commands first. The server comes from the API you signed
in to with `oci auth login`, or `OUTCOMECI_API_URL`.

## Slack

`oci integration slack setup` creates and installs a Slack app with the
official [Slack CLI](https://docs.slack.dev/tools/slack-cli/), with only the
scopes the Slack connector's operations use. For a webhook trigger, run it
again with `--request-url` once the app's signing secret is in the Vault.
`oci integration slack sync-credentials` copies the app's bot token into the
local Vault (`--local`) or a workspace Vault (`--cloud --workspace-id ID`).

## Publish a reusable workflow

```console
oci workflow prepare-publication outcome.yml --output /tmp/public --sensitive-term "Acme"
```

The command uses a locally installed, authenticated agent (Codex by default;
override with `--agent`) to replace identities, Vault paths, repositories and
other consumer-specific values in a copy of the workflow. It writes typed setup
requirements, a replacement report and a publication overview under `.outcomeci/`,
then rejects the package if privacy checks or the compiler fail.

## Contributing

Start with the [source map and execution walkthrough](CONTRIBUTING.md#source-map).
[CONTRIBUTING.md](CONTRIBUTING.md) also covers setup and the formatting, lint,
test and package checks CI runs.
