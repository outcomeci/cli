# OutcomeCI CLI

`oci` writes, runs and publishes OutcomeCI workflows. A workflow is one
`outcome.yml` file in the `outcomeci.workflow/v1` format: what starts a run,
the APIs its steps may call, and its agent, model and runtime-driven steps. You iterate
on it locally, running it in the same runner container OutcomeCI Cloud uses,
then sync it to OutcomeCI Cloud where triggers run it on managed runners.

The starter needs Python 3.11 or newer, `pipx`, Docker or Podman, and a
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
  `agent` (or the deprecated `runner` alias) is an agent; one with only a `model`, such as
  `light: {model: anthropic/claude-haiku-4-5}`, reasons with direct model calls
  and no workspace, its granted APIs offered as tools. `review` sets the policy
  reviewer's model. A model profile's `auth: secrets.<name>` (or legacy `key`) selects your
  provider key. Managed Cloud runs can use platform keys for OpenAI and
  Anthropic when `key` is omitted; other chat providers require an explicit
  key. Policy-review model profiles support OpenAI and Anthropic.
- `steps`: agent or model steps with grants (`can:`) and an optional `policy:`, `await`
  steps that wait for a human signal, and `converse` steps that discuss a plan
  in a thread, and `decision` steps for typed model decisions. Only workflows
  with `type: dispatcher` can use `dispatch` steps to queue child workflows.
  Dispatching children requires managed Cloud execution.

`oci validate` compiles the workflow and prints its revision; `oci workflow
compile` prints the compiled steps, grants, instructions and schemas.

Agent profiles can select a named Vault credential as well:

```yaml
secrets:
  coder: vault:agents/engineering-codex
reasoning:
  default:
    agent: codex
    auth: secrets.coder
```

The Vault entry has `credential_type: agent`, `configuration.provider: codex`,
and `secrets.auth_json` containing the Codex login JSON. Claude uses
`configuration.provider: claude` and `secrets.oauth_token`; OpenCode uses
`configuration.provider: opencode` and `secrets.api_key`. Local runs resolve the
same typed entry from the local Vault. Cloud runs lease the named identity and
save rotated credentials back to that entry. Agent credentials are never supplied
to model calls or the API capability broker.

`agent` and `runner` cannot appear together. Model profiles, their fallbacks,
and `training.labeler` accept `auth` or `key`, but not both. Each workflow can
use one identity per agent provider; different providers can use different
identities. Reusing the same identity is supported. Mixing different explicit
credentials, or explicit and implicit credentials for the same provider, is
rejected. A profile inherits the default credential only when it uses the same
provider. Omitted authentication retains local login discovery or cloud Vault
selection.

Tavily search supports a platform key in managed Cloud runs:

```yaml
apis:
  search: {uses: tavily}
```

Set `auth: secrets.tavily` and declare `tavily: vault:tavily/key` under
`secrets` to use your own key. Managed runs proxy both key sources through the
API and record search credit usage without exposing keys to runners. Local
runs require your own key. `search.search` accepts `query`, `max_results`,
`topic`, `search_depth`, `include_domains` and `exclude_domains`; grant-fixed
fields are filled in by the broker. Tavily grant constraints must be static
values. A decision can select conditional search steps with different domain
or topic grants; see [the Slack research example](tests/examples/slack-research/outcome.yml).

An API without a first-party connector binds with `custom`. The workflow
names the origin, and a static Vault credential (a token, an api key, or
basic auth) signs each request:

```yaml
secrets:
  acme: vault:acme/token
apis:
  acme: {uses: custom, base_url: https://api.acme.com, auth: secrets.acme}
steps:
  - sync:
      reason: Reconcile open orders.
      can:
        - acme.read: {path: /v1/orders}
        - acme.write: {path: /v1/orders}
```

`base_url` is an https origin: no path, query or userinfo. `acme.read` sends
GET; `acme.write` sends POST, PUT, PATCH or DELETE and only from a step that
grants it. A `path` grant limits requests to that prefix; without one, any
path on the origin. An `api_key` credential names its header with
`--header-name` (default `X-API-Key`) and an optional `--prefix`. Every
request is pinned to the origin, follows no redirects, refuses private
addresses and cannot set Authorization, Cookie, Host or Proxy-Authorization.
A local run may set `allow_private_network: true` on the binding to reach a
private or `http` origin; managed Cloud runs refuse such a workflow.

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
inside a local Docker or Podman container. This remains local execution with `--cloud`.
Released CLIs select the matching runner image; development builds need
`--image IMAGE`.

Docker is selected when installed; otherwise the CLI uses Podman. Select explicitly
with `--runtime docker` or `--runtime podman`. On SELinux hosts, the CLI labels
disposable workflow copies and output directories, preserving the checkout's labels.
Rootless Podman runs preserve the host user's file ownership. See the
[Red Hat compatibility lab](scripts/redhat/README.md) for repeatable host checks.

Rancher Desktop needs its dockerd (moby) engine. Select its Docker context with
`DOCKER_CONTEXT=rancher-desktop oci workflow run ...`. The CLI uses Rancher's shared
scratch directory for its private source copy and output. Run the
[Rancher Desktop checks](scripts/rancher/README.md) to verify the setup.

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

For a workspace that requires SSO, authenticate for that workspace:

```sh
oci auth login --workspace-id workspace_example
```

The browser completes the workspace's identity-provider verification before approving the
CLI. Workspace SSO expires independently of token refresh. If a command requests
SSO again, repeat the workspace login, then rerun the command. Use `--no-open` to
copy the browser link manually and `--api-url URL` for staging. Workspace API keys
are automation credentials and require explicit owner authorization when SSO is
enforced; they cannot be combined with `--workspace-id` during login.

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

### Google workflow credentials

Create Google credentials in the workspace Vault and select the Drive or Analytics
permissions that each workflow needs. You can create multiple entries for different
accounts or grants. Managed credentials use OutcomeCI's OAuth app: the API retains
refresh credentials and leases short-lived access tokens to the runner. Account
sign-in does not grant workflow API access. Existing user-owned OAuth apps and
service accounts remain supported.

Use `google.drive` for folder-scoped listing, file metadata, binary downloads and
Google Workspace document exports, folder creation and file metadata updates. Use
`google.analytics` for GA4 reports. Both use the same Google module and can refer to
the same Vault entry when it has the necessary permissions. Downloads are saved as
run artifacts after grant checks; Drive downloads are limited to 20 MiB and exports
to 10 MiB. This does not provide arbitrary file content editing or sharing.

This release pins `outcomeci-connectors==0.12.0`. Existing workflows with locked Google
Analytics connector digests must refresh their connector lock to accept bearer
authentication; existing OAuth and service-account methods remain available.

## Query previous step outputs

A step can opt its validated `returns:` into local or cloud output history with
`index.fields`. Existing JSON output artifacts remain authoritative. Each workflow
has its own SQLite projection; cloud execution publishes immutable snapshots to
private S3 storage. History is not automatically shared between workflows or
uploaded from a local project.

Declare sources at workflow scope to limit accessible steps, returned fields and
result counts. Put a query under `outputs.queries` for shared inputs, or under a
step's `query` for inputs specific to that step. Reference a result explicitly with
`history.<query_name>` in `with`; declaring a source does not inject its contents.

```yaml
apiVersion: outcomeci.workflow/v1
trigger: manual
outputs:
  sources:
    history:
      workflow: self
      steps: [assess]
      fields: [customer_id, decision, summary]
      max_results: 10
  queries:
    previous:
      source: history
      where:
        customer_id: {eq: {ref: trigger.customer_id}}
      order_by: [{field: completed_at, direction: desc}]
      limit: 3
steps:
  - assess:
      reason: Assess the customer using the request and prior assessments.
      with: [trigger, history.previous]
      returns:
        customer_id: string
        decision: string
        summary: string
      index:
        fields: [customer_id, decision]
```

Only indexed fields and supported system metadata can be filtered or sorted.
Source `fields` also limits which return values are disclosed; a summary may be
returned without being indexed. Predicates support `eq`, `lt`, `lte`, `gt`, `gte`
and explicit `is_null`, with strict scalar types. Strings are literals; use
`{ref: trigger.field}` for a dynamic value. Step queries can also reference completed
predecessors. Queries cannot expand source permissions.

A run pins its history snapshot before work starts. New publications do not change
its inputs, including after resume. The current run is excluded; use normal step
references for its outputs. Sources default to `run_status: succeeded`; explicitly
choose `step_succeeded` to include accepted outputs from runs that subsequently
failed. Empty queries return a list; `required: true` makes no matches an input
error. Deleted or unavailable evidence fails explicitly rather than selecting a
newer output.

Local inspection and recovery use the same source constraints:

```console
oci outputs query --source history --query '{"where":{"customer_id":{"eq":"customer_123"}},"limit":3}'
oci outputs rebuild
```

Both commands accept `--dir` and `--config`. Rebuild replays retained output evidence
without invoking models or repeating workflow actions; missing sources are reported.
Query results include source references and bounded pagination. Limits are 100 rows
and 256 KiB per query, with 20 queries and 1 MiB of historical inputs per run.
Indexing failures leave JSON artifacts intact and report publication availability
separately. Workflow definitions without indexing keep their existing behavior.

Local workflow identities are stored privately by project-relative configuration
path. Renaming the configuration starts separate history; it does not silently
combine databases. Source grants restrict query results, not access to artifacts
already present in a trusted local workflow process or the same managed run.
Managed runners query the API and never receive the raw history database.

### Workflow objectives and training annotations

V1 workflows can optionally declare an `objective` and a `training.labeler`:

```yaml
apiVersion: outcomeci.workflow/v1
objective: Identify the root cause and propose a concrete fix.
trigger: manual
training:
  labeler:
    model: openai/gpt-4.1-mini
    review_below: 0.6
steps:
  - investigate:
      reason: Investigate the reported issue and propose a fix.
```

The objective gives the labeler the workflow's intended outcome. After a run
completes, the labeler writes a separate training annotation; a labeler failure
does not fail the completed run. Optional `review_below` routes annotations with
lower confidence to human review and must be between 0 and 1. The labeler accepts
the same model credentials as other model profiles. Both fields are optional.
Use `apiVersion: outcomeci.workflow/v1`; v2 is no longer supported.

To use a decision model for labeling, opt in with `mode: decision`:

```yaml
apiVersion: outcomeci.workflow/v1
objective: Identify the root cause and propose a concrete fix.
trigger: manual
secrets:
  judge: vault:models/typesafe
training:
  labeler:
    mode: decision
    model: typesafe/jev-latest
    key: secrets.judge
    review_below: 0.6
steps:
  - investigate:
      reason: Investigate the reported issue and propose a fix.
```

Decision mode supports `typesafe/<model>` with your Vault key, or
`openai/gpt-6-luna` with an optional Vault key. It uses the existing Decisions
transport locally and in managed runs; local runs need `outcomeci-cli[models]`.
Decision mode does not support model fallback. Omit `mode` (or use `mode: llm`)
to keep the existing LLM labeler and its generated explanations and labels.

Decision labelers use a fixed rubric: outcome (`success`, `partial`, `failure`)
and primary failure category (`none`, `missing_output`, `incorrect_output`,
`incomplete_execution`, `insufficient_evidence`, `other`). The annotation keeps
both answers and their probability distributions under `decisions`. Its
`confidence` is the lower of the two answer confidences, and `review_below`
uses that value. This aggregation is a review rule, not a measured probability
that the whole annotation is correct. Choose the model and threshold using your
own evaluation data.

Decision annotations have empty `explanation` and `evidence` fields because
these models do not generate prose. `labels` contains the selected failure
category, or is empty for success. The transcript retains the supplied run
record, rubric, provider response, and token usage. Refusals, malformed answers,
and contradictory outcome/category pairs fail only the annotation, leaving the
completed run unchanged. No additional LLM is called to explain a decision.
