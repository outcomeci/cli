# OutcomeCI CLI

`oci` installs and executes the open-source Standup framework used by OutcomeCI.

```console
pip install outcomeci-cli
oci init
oci validate
oci outcome run --claim /workspace/outcome-claim.json --workspace /workspace
```

Repository context and workflow instructions live in `.outcomeci/`. The same
`outcome.yml` contract can run locally or use OutcomeCI Cloud for managed state,
Digital Twin context, expert messaging, credentials, and runners.

## Local HTTP tunnel preview

The development tunnel service exposes explicitly approved loopback HTTP ports.
Authenticate to the local API, then keep the client running in the foreground:

```console
oci tunnel start --workspace-id WORKSPACE_ID --target http://127.0.0.1:3000 --public
oci tunnel status --workspace-id WORKSPACE_ID
oci tunnel stop --workspace-id WORKSPACE_ID
```

The client downloads frpc 0.71.0 from its official release and verifies its pinned
checksum. Its configuration stays in a private broker directory, not workflow
artifacts. Session credentials never appear in status or agent context. Ctrl+C
revokes that exact session; reconnect does not extend the lease. The default
duration is 15 minutes, with `--ttl-seconds` up to one hour.

Local testing requires the separate local tunnel service: send
`Host: ASSIGNED_HOSTNAME` to `http://127.0.0.1:7402`. Cloud sessions use HTTPS
under `tunnel.outcomeci.com` (staging: `staging.tunnel.outcomeci.com`) once the
environment is deployed. Public client connections use WSS with system CA
trust roots and explicit hostname verification; insecure remote TCP is rejected.
User application WebSocket upgrades remain unsupported. `--public`
acknowledges exposure; a hard-to-guess hostname is not authentication. SQS queued
workflow triggers remain independent.

Every workflow declares at least one trigger. Local interactive execution uses
an explicit manual trigger; managed workflows may start from durable events:

```yaml
spec:
  triggers:
    manual:
      type: manual
    inbound_email:
      type: email.received
      filters:
        subject_prefix: Customer outcome
```

Phase inputs reference the immutable trigger payload with
`from: trigger.inbound_email`. Email payloads contain safe metadata and
encrypted artifact references, not plaintext attachments or encryption keys.

To run the discovery phases with a locally installed Codex or Claude Code:

```console
oci init --backend filesystem
oci outcome compile outcome.yml
oci outcome start "Describe the outcome you want"
oci outcome status
oci outcome continue <run-id> --approve
```

`start` performs intake. Each approved `continue` advances to plan and then
tasks. Local state, artifacts, manifests, transcripts, and usage records are
written beneath `.outcomeci/outcomes/<run-id>/`. Runner and model defaults come
from `outcome.yml`; `--agent` and `--model` provide per-invocation overrides.

`oci init` also installs repository-local Outcome skills for Codex and Claude
Code. They use the current interactive session rather than launching a child
process:

```text
Codex:       $outcome Describe the outcome you want
Claude Code: /outcome Describe the outcome you want
```

The skill coordinates `outcome begin`, `compile`, `validate-artifacts`,
`advance`, and `status`. Managed OutcomeCI runners continue to use `outcome
run --claim ...`; both modes share workflow compilation and artifact schemas.

## Ecosystem durability proofs

The separately packaged `proof-runner` treats a versioned persona journey as
an ecosystem-level test. Its bundled local-first proof starts with an empty
workspace, initializes OutcomeCI and an encrypted local Vault, executes a
Vault-backed capability, runs intake through tasks, kills phase processes at
durable boundaries, and verifies exact recovery:

```console
oci proof run --workspace ./proof-runs
docker build -f Dockerfile.proof-runner -t outcomeci-proof-runner .
docker run --rm --network none --tmpfs /proof:rw,noexec,nosuid,uid=10001,gid=10001,size=128m outcomeci-proof-runner
```

Pass/fail evidence is written as a machine-readable report and hash-linked
event ledger. `proof.yml` holds exactly one persona journey. The first release
ships `local-first-v1` for offline runtime durability and `email-trigger-v1`
for managed ingress. The managed proof uses only a workspace API key and sends
a fixed MIME message through the real SES ingress:

```console
export OUTCOMECI_PROOF_API_URL=https://staging-api.outcomeci.com
export OUTCOMECI_PROOF_WORKSPACE_ID=workspace_example
read -rsp "Workspace API key: " OUTCOMECI_PROOF_API_KEY
export OUTCOMECI_PROOF_API_KEY
oci proof run --name email-trigger-v1 --workspace ./proof-runs
```

It waits for encrypted artifact persistence, exactly-once workflow invocation,
and real metered usage, then prints a redacted `email received` receipt.

Filesystem workflows can pin repository evidence explicitly:

```yaml
spec:
  context:
    provider: filesystem
    include:
      - .outcomeci/context/**
      - docs/**
    exclude:
      - node_modules/**
      - dist/**
```

Compiled context records each matched path, size, and SHA-256 hash. Context
changes therefore produce a new workflow revision. Files remain in place and
are read by the active local agent; they are not copied into `outcome.yml`.

## Credential-blind API capabilities

An outcome workflow can grant named API operations only to phases that need
them. It stores a logical credential reference, never its value. The agent
supplies operation input and receives only explicitly exposed response fields.

The packaged schema is available without network access:

```sh
oci schema path
oci schema export ./outcome.schema.json
```

Pin and verify the fully resolved workflow, including integration packages:

```sh
oci outcome lock
oci outcome verify-lock
oci conformance --workflow ./outcome.yml
```

For an entirely offline runtime, create an encrypted local Vault and reference
logical paths from connections:

```sh
oci vault local init
oci vault local put linear/api_key
oci vault local list
```

```yaml
auth: {type: bearer, credential: vault:linear/api_key}
```

The encrypted workspace file and its AES-256-GCM key are stored separately.
Containers receive the key through a read-only file mounted at the path named
by `OUTCOMECI_VAULT_KEY_FILE`; agent sandboxes cannot read that mount.

```yaml
spec:
  agents:
    phases:
      intake:
        instructions: .outcomeci/instructions/intake.md
        needs: []
        integrations:
          - type: api
            capability: linear.create_issue
            required: true

  connections:
    linear:
      provider: http
      base_url: https://api.linear.app
      auth:
        type: bearer
        credential: env:LINEAR_API_KEY

  integrations:
    linear:
      connection: linear
      access: {mode: schema}
      operations:
        create_issue:
          description: Create a Linear issue
          input:
            type: object
            required: [query, variables]
            properties:
              query: {type: string}
              variables: {type: object}
            additionalProperties: false
          request:
            method: POST
            path: /graphql
            body:
              query: "{{ input.query }}"
              variables: "{{ input.variables }}"
          response:
            expose:
              issue: body.data.issueCreate.issue
              errors: body.errors
```

```console
oci integration list --phase intake
oci integration describe linear.create_issue
printf '%s' '{"query":"...","variables":{}}' |
  LINEAR_API_KEY=... oci integration execute linear.create_issue \
    --phase intake --input-stdin
```

Parameters stay dynamic while the workflow fixes the origin, method,
credential, schema, phase grant, and response projection. Local credentials use
an explicit `env:` reference; cloud runners use a workflow-scoped Vault lease.

The authoring modes are `schema` for declared operations, `openapi` for a
specification URL plus operation-ID allowlist, and `full` for intentional
broad discovery against a fixed origin and method allowlist. Full mode grants a
single `<integration>.request` capability; it can vary relative paths,
permitted methods, query values, safe headers, and bodies but cannot change the
origin, credential, or response exposure policy. OpenAPI operations are
materialized into a child workflow before execution.

### Learned-operation lineage

`oci integration patch propose` turns a discovered request into an
`OutcomeWorkflowPatch` with its parent revision, run, phase, agent, and
reason. `oci integration patch apply` rejects stale parents and writes a
separate child workflow; it never edits the current version. Sync that child
with `oci workflow sync --version` to create its immutable cloud version.

## Custom workflows

The default intake, plan, tasks, and implementation shape is a starting point,
not a fixed methodology. Keep compatible lifecycle phases while replacing the
orchestrator, instructions, artifact contracts, and human checkpoints:

```yaml
instructions:
  product_standup:
    path: .outcomeci/instructions/product-standup.md
agents:
  default:
    runner: codex
  phases:
    intake:
      instructions: .outcomeci/instructions/product-discovery.md
      needs: []
      expects:
        inputs:
          - {name: product_request, from: runtime.intent, media_type: text/plain}
        outputs:
          - {name: trajectory, path: intake/trajectory.json, media_type: application/json}
          - {name: experience_brief, path: intake/experience.md, media_type: text/markdown}
      integrations:
        - type: human
          timing: during
          id: clarify_experience
          participant: product_owner
          purpose: Resolve a consequential product ambiguity.
          interaction: consultation
          required: true
          availability: on_demand
          delivery:
            type: slack
            connection: slack_local
            targets: [{kind: user, name: izzy}]
          wait: {strategy: ask}
    plan:
      instructions: .outcomeci/instructions/delivery-design.md
      needs: [intake]
      expects:
        inputs:
          - {name: experience_brief, from: intake.outputs.experience_brief, media_type: text/markdown}
        outputs:
          - {name: delivery_design, path: plan/delivery-design.md, media_type: text/markdown}
```

Each referenced Markdown file defines how its role works. Compilation resolves
the graph and exact input/output handoffs before any agent runs.

## Local Slack participation

OutcomeCI can create and install a user-owned Slack app without an OutcomeCI
Cloud account. Install the official [Slack CLI](https://docs.slack.dev/tools/slack-cli/),
initialize a filesystem workflow, and run:

```console
oci integration slack setup --name "Acme Outcomes"
oci integration slack status
oci human targets
```

`setup` delegates workspace authorization, manifest validation, and app
installation to Slack CLI. Slack may ask you to run an authorization ticket in
your workspace and approve the new app in a browser. OutcomeCI generates the
Slack project metadata directly, so setup will not ask whether to link an
existing app. The generated project lives in `.outcomeci/integrations/slack/`.
Human hooks use short-lived `slack api` calls, so they need no public webhook,
long-running listener, or OutcomeCI backend.

By default, Slack credentials remain in Slack CLI's own credential store. When
ready to use the installed app from an HTTP workflow integration, sync its bot
token into an encrypted local Vault or an authenticated cloud workspace Vault:

```console
oci integration slack sync-credentials --local
oci integration slack sync-credentials --cloud workspace_… --workflow WORKFLOW_ID
```

`--workspace PATH` selects the workflow directory containing the generated Slack
app (defaults to the current directory). For a different local destination, add
`--vault-workspace PATH`. Use `--team TEAM_ID_OR_DOMAIN` when multiple workspaces
have installed the app. Local Vault initialization is automatic. Both destinations
use `slack/bot-token` unless you supply `--path`; repeat syncs replace the local
value or create a new cloud secret version. Cloud rotation preserves existing
workflow grants unless `--workflow` is explicitly supplied, which replaces them.
New cloud secrets have no workflow grants unless you supply them.

The command refreshes Slack CLI authorization, obtains the selected installed
app's bot token through the same Slack installation API used by `slack api --app`,
and verifies its app and workspace before storing it. It uses the installed app's
scopes, never edited local scopes. Tokens stay out of output, command arguments,
and temporary files. This relies on Slack's `apps.developerInstall` API; Slack CLI
credentials and that API may change between Slack versions. If authorization
has expired and cannot refresh, run `slack login` and retry.

An HTTP connection can then use the stored credential:

```yaml
slack:
  provider: http
  base_url: https://slack.com
  auth: {type: bearer, credential: "vault:slack/bot-token"}
```

Provider-neutral credentials can be written directly without placing their
value in shell history. Declare the provider and authentication contract; the
CLI supplies safe Authorization/Bearer defaults for `auth_header`:

```console
printf '%s' "$SLACK_BOT_TOKEN" | oci vault put slack/bot-token \
  --workspace-id workspace_abc123 \
  --name "Slack bot token" \
  --provider slack \
  --credential-type auth_header \
  --workflow WORKFLOW_ID \
  --value-stdin
```

Other supported credential contracts are `api_key`, `oauth2`, and `oidc`.
Use `--header-name`, `--prefix`, `--scheme`, `--token-url`, `--issuer-url`,
`--client-id`, `--grant-type`, repeated `--scope`, and `--audience` to describe
their non-secret configuration. Use `--secrets-json-stdin` for contracts such
as OAuth refresh-token grants that require more than one secret field. Typed
credentials deliberately reject secret values supplied in process arguments.

Syncing does not modify workflow connections or enable a cloud runner to read
a local Vault. Local execution uses the local Vault; cloud execution needs the
cloud secret and a grant for its workflow.

For human hooks, OutcomeCI adds only this non-secret reference to `outcome.yml`:

```yaml
connections:
  - ref: slack_local
    provider: slack
    delivery: on_demand
```

To deliver a human interaction through Slack, declare Slack delivery on the
phase hook. This is `mode: message` (the default, so it can be left out) --
the agent-driven `oci human request`/`poll`/`accept` lifecycle; see
"Reaction delivery" below for the other mode, a runtime-native fast path for
a single approve-or-timeout gate.

```yaml
integrations:
  - type: human
    timing: after
    id: confirm_scope
    participant: requester
    purpose: Confirm the intent and affected scope before planning.
    interaction: approval
    required: true
    delivery:
      type: slack
      connection: slack_local
      targets:
        - kind: user
          name: izzy
        - kind: channel
          name: product
    wait:
      strategy: ask
```

Discover and assign people without copying Slack IDs:

```console
oci human targets
oci human assign intake after confirm_scope --user izzy --channel product --wait ask
```

Agents deliver and poll hooks through deterministic commands:

```console
oci human request confirm_scope --run <run-id>
oci human poll confirm_scope --run <run-id> --wait 300
oci human accept confirm_scope "Use the existing navigation" --run <run-id>
```

The CLI privately resolves readable names to Slack transport identifiers. IDs
never enter `outcome.yml`, agent prompts, command output, or outcome artifacts.
When `wait.strategy` is `ask`, Codex or Claude Code asks whether to wait for a
bounded duration or continue. Polled replies are stored with the interaction
and become context for subsequent phases.

During local agent execution, OutcomeCI uses Bubblewrap to mount workflow
configuration read-only, hide Slack CLI credentials, and expose only the
current outcome artifact directory as writable. A short-lived Unix-socket
capability permits only the current phase's configured hook IDs. Human
responses are re-read from Slack before acceptance. Execution fails closed if
Bubblewrap is unavailable; prompts are not treated as a security boundary.

### Custom human transports

A human hook may use an internal HTTP service or MCP server without granting
the agent arbitrary access to it. The adapter always exposes the same
`request`, `poll`, and verified `accept` lifecycle:

```yaml
connections:
  - ref: people_api
    provider: custom
    transport:
      type: http
      endpoint: https://people.example.com
    auth:
      env: PEOPLE_API_TOKEN
    operations:
      request: {method: POST, path: /requests}
      poll: {method: GET, path: /requests/{correlation_id}}
    contract:
      request:
        input:
          type: object
          required: [run_id, phase, interaction_id, interaction, purpose, targets]
        output:
          type: object
          required: [correlation_id]
      poll:
        output:
          type: object
          required: [responses]
```

Set `delivery.type: custom` and `delivery.connection: people_api` on the human
hook. The request operation returns `correlation_id`. Poll returns
`responses`, where each item contains readable `from`, `message`, and
`responded_at` strings. MCP connections use `transport.type: mcp`, protocol
`streamable_http` or `stdio`, and tool names under `operations.request.tool`
and `operations.poll.tool`.

### Reaction delivery

A `before` approval hook can resolve itself by polling for a Slack reaction,
with no `oci human request`/`poll`/`accept` involved. Set `delivery.mode:
reaction` on a `type: slack` hook -- there is no separate `type: reaction`.
The runtime resolves it directly, the same way locally and in OutcomeCI
Cloud:

```yaml
integrations:
  - type: human
    timing: before
    id: approve_fix
    participant: {role: approver}
    purpose: Approve opening a fix PR.
    interaction: approval
    delivery:
      type: slack
      mode: reaction
      source: notify.outputs.delivery
      emoji: "+1"
      poll_interval_seconds: 20
    on_timeout: fail
    required: true
    wait: {strategy: block, timeout_seconds: 300}
  - type: api
    capability: slack.get_reactions
    required: false
```

Reaction mode needs no `connection` (unlike `mode: message`, below) -- it
resolves its Slack access through the phase's own `slack.get_reactions` API
capability grant instead.

`source` is `<phase>.outputs.<name>`, a direct `needs` dependency's own
declared output holding the `channel` and `ts` of the message to watch. The
phase also needs a `<connection>.get_reactions` capability (a plain
schema-mode `GET /api/reactions.get` operation) declared `required: false` --
the runtime's poll calls bypass the broker/effect-confirmation path entirely,
so a `required: true` capability here can never be confirmed and always fails
the phase.

The runtime polls every `poll_interval_seconds` (default `20`) for up to
`wait.timeout_seconds` (default `300`) for the configured `emoji` (default
`+1`). `on_timeout: fail` (default) fails the run; `on_timeout: continue`
resolves the hook without approval and lets the phase run anyway. Only
`timing: before` and `interaction: approval` are supported -- there is no
reject path, only unblock-or-timeout.

Credentials remain in the named environment variable on the host side of the
capability broker. OutcomeCI removes every connection-declared credential from
the agent environment.

### Reply delivery

A consultation hook can resolve itself the same way reaction delivery does --
no `oci human request`/`poll`/`accept`, no local Slack CLI, no `provider:
slack` connection. Set `delivery.mode: reply` on a `type: slack` hook. The
runtime polls the same message's thread for a new, non-bot reply and resolves
the hook with that reply's actual text, the same way locally and in OutcomeCI
Cloud:

```yaml
integrations:
  - type: human
    timing: before
    id: plan_consultation
    participant: {role: requester}
    purpose: Discuss and refine the proposed plan before implementation.
    interaction: consultation
    delivery:
      type: slack
      mode: reply
      source: plan.outputs.delivery
      poll_interval_seconds: 20
    on_timeout: continue
    required: false
    wait: {strategy: block, timeout_seconds: 1800}
  - type: api
    capability: slack.get_replies
    required: false
```

Like reaction mode, reply mode needs no `connection` -- it resolves its Slack
access through the phase's own `slack.get_replies` API capability grant, and
`source` is `<phase>.outputs.<name>`, a direct `needs` dependency's own
declared output holding the `channel` and `ts` of the message whose thread to
watch (the agent sends that message itself, e.g. via a `slack.send_message`
capability call, in an earlier phase this one `needs`). Like reaction, reply
mode only supports `timing: before` -- the runtime resolves both modes
synchronously before the gated phase starts, and that's currently the only
call site that threads through what resolving them needs. Reply requires
`interaction: consultation` rather than `approval` -- there is no emoji to
match, so any new human reply resolves it.

The resolved reply's text becomes that interaction's `response.message`,
automatically part of every later phase's context -- no special file to read.
`on_timeout: continue` (unlike reaction's `fail` default) is usually what you
want here: a consultation with no reply shouldn't hard-fail the run, just let
the deciding phase treat "no reply" as its own outcome.

## Sync a workflow to OutcomeCI Cloud

Authenticate this machine through your signed-in browser:

```bash
oci auth login
```

For non-interactive CLI or MCP access, create a member key on the workspace
Access screen and pass it over stdin so it is not written to shell history:

```bash
oci auth login --key-stdin
```

Paste the key at the protected prompt. Automation can pipe the key over stdin.

Workspace keys inherit the member's current Workflow and Vault permissions.
They are limited to one workspace, can be revoked from Access, and are never
refreshed as user sessions.

Fetch a workflow's current revision, including its `.outcomeci/` support
files, to edit locally and push back:

```bash
oci workflow get WORKFLOW_ID --workspace-id workspace_abc123 --output outcome.yml
```

Then create the first immutable workflow revision for a workspace:

```bash
oci workflow sync outcome.yml \
  --workspace-id workspace_abc123 \
  --create
```

When that workflow already exists, make the versioning intent explicit:

```bash
oci workflow sync outcome.yml \
  --workspace-id workspace_abc123 \
  --version
```

The CLI validates the complete local workflow and uploads a bounded bundle of
`outcome.yml`, the constitution, and referenced `.outcomeci/` support files.
OutcomeCI-managed runs hydrate and version that bundle without requiring a Git
state repository. A GitHub state repository is used only when the workflow
selects it explicitly. Credentials live in
`~/.config/outcomeci/credentials.json` with owner-only permissions and never in
the outcome repository. Set `OUTCOMECI_API_URL` when testing against a local or
staging control plane.

## Verify a reusable workflow package locally

OutcomeCI Cloud prepares public versions with the configured coding agent and
pauses for approval. Maintainers can exercise the same sanitizer and compiler
contract locally:

```bash
oci workflow prepare-publication outcome.yml \
  --output /tmp/my-workflow-public \
  --agent codex \
  --sensitive-term "Acme Corporation"
```

The output directory must be empty. The command replaces identities, Vault
paths, connection references, repositories, endpoints, and other values a new
consumer must provide. It emits typed setup requirements and a replacement
report under `.outcomeci/`, then rejects the package if privacy checks or the
pinned workflow compiler fail. Reports never contain the original values.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the supported Python versions and the
formatting, lint, test, and package checks used by CI.

### Permission advisor execution logs

A queued local workflow records integration proposals, advisor allow/revise/deny
reasons, request start and result summaries, and static permission denials. The
listener streams these events through its execution heartbeat to the existing
portal workflow log. Events include phase, capability, proposal digest, method,
endpoint and purpose; credentials, raw provider IDs, and request/response bodies
are excluded. Stable event IDs make upload retries idempotent. Final events are
flushed on success and failure. If the API has not advertised policy-event
support, evidence remains in the private local broker journal. Upgrade the API
before expecting cloud logs. A final upload failure is reported while keeping
the local journal; it never authorizes replaying an integration effect.
