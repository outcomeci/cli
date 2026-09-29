# OutcomeCI CLI

`oci` writes, runs and publishes OutcomeCI workflows. A workflow is one
`outcome.yml` file in the `outcomeci.workflow/v1` format: what starts a run,
the APIs its steps may call, and the steps an agent works through. You iterate
on it locally, running it in the same runner container OutcomeCI Cloud uses,
then sync it to OutcomeCI Cloud where triggers run it on managed runners.

```console
pipx install outcomeci-cli
oci init
oci vault local init
oci vault local put github --value-stdin     # a GitHub token that can read the repo
oci workflow run --payload .outcomeci/request.json
```

The full guide is at <https://outcomeci.com/docs/outcomeci>.

## Write a workflow

`oci init` writes a starter `outcome.yml`, its step instructions under
`.outcomeci/instructions/`, and a sample request. A workflow declares:

- `trigger`: `manual`, `email`, `webhook` (optionally verified by a provider,
  such as signed Slack events), or a `cron` schedule.
- `secrets`: Vault references, such as `github: vault:github`.
- `apis`: a connector bound to a secret, such as
  `github: {uses: github, auth: secrets.github}`. Connectors come from
  [`outcomeci-connectors`](https://github.com/outcomeci/connectors).
- `reasoning`: the default agent (`codex`, `claude` or `opencode`) and a fallback.
- `steps`: agent steps with grants (`can:`) and an optional `policy:`, `await`
  steps that wait for a human signal, and `converse` steps that discuss a plan
  in a thread.

`oci validate` compiles the workflow and prints its revision; `oci workflow
compile` prints the compiled steps, grants, instructions and schemas. See
[`examples/v1`](examples/v1) for two complete workflows.

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

The same options work for the local Vault (`oci vault local put`) and the
workspace Vault (`oci vault put`), so a credential resolves the same way in
both. Pass secret fields on stdin: `--value-stdin` for one value, or
`--secrets-json-stdin` for a JSON object such as
`{"username": "...", "password": "..."}`.

Credentials never reach an agent. Steps call APIs through a broker that
resolves the credential, checks the call against the step's grants, and sends
it. A provider that rotates a refresh token revokes the old one, so the runtime
saves the new one to the Vault the credential came from before it is used again.

## Run a workflow

`oci workflow run` runs the workflow in `--dir` inside the runner container. It
needs Docker.

- With no cloud options, secrets come from the local Vault and the agent login
  from this machine: Codex from `~/.codex/auth.json`, Claude from
  `CLAUDE_CODE_OAUTH_TOKEN` or the local Vault entry `agents/claude`, and
  OpenCode from `OPENROUTER_API_KEY` or `agents/opencode`.
- `--cloud WORKFLOW_ID --workspace-id ID` leases the workspace Vault's
  credentials and its connected agent instead.

`--payload FILE` supplies the trigger payload, `--auto-continue` runs every
step, and `--retry RUN_ID` resumes a run that stopped on an error. Each run's
state lands in `.outcomeci/outcomes/<run>/`, and every API call and its outcome
in `.outcomeci/.broker/<run>/journal.json`.

## Take it online

```console
oci auth login
oci vault put github --workspace-id ID --value-stdin
oci workflow sync outcome.yml --workspace-id ID --create
```

`oci workflow sync` compiles the workflow first, then uploads it with its
`.outcomeci/` support files. It never uploads the local Vault, the broker
journal or run outputs. Grant each Vault entry to the workflow with
`oci vault grant`. Use `--version` to add a version to an existing workflow and
`oci workflow get` to fetch the latest cloud revision.

Credentials are stored in `~/.config/outcomeci/credentials.json`, or under
`OUTCOMECI_CONFIG_HOME` when it is set. Set `OUTCOMECI_API_URL` to use another
OutcomeCI API.

## Slack

`oci integration slack setup` creates and installs a Slack app with the
official [Slack CLI](https://docs.slack.dev/tools/slack-cli/), with only the
scopes the Slack connector's operations use. For a webhook trigger, run it
again with `--request-url` once the app's signing secret is in the Vault.
`oci integration slack sync-credentials` copies the app's bot token into the
local Vault (`--local`) or a workspace Vault (`--cloud ID`).

## Publish a reusable workflow

```console
oci workflow prepare-publication outcome.yml --output /tmp/public --sensitive-term "Acme"
```

The command replaces identities, Vault paths, repositories and other values a
new consumer provides, writes typed setup requirements and a replacement report
under `.outcomeci/`, and rejects the package if privacy checks or the compiler
fail. Reports never contain the original values.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the supported Python versions and
the formatting, lint, test and package checks CI runs.
