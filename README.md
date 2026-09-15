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
oci tunnel start --workspace WORKSPACE_ID --target http://127.0.0.1:3000 --public
oci tunnel status --workspace WORKSPACE_ID
oci tunnel stop --workspace WORKSPACE_ID
```

The client downloads frpc 0.71.0 from its official release and verifies its pinned
checksum. Its configuration stays in a private broker directory, not workflow
artifacts. Session credentials never appear in status or agent context. Ctrl+C
revokes that exact session; reconnect does not extend the lease. The default
duration is 15 minutes, with `--ttl-seconds` up to one hour.

Local testing requires the separate local tunnel service: send
`Host: ASSIGNED_HOSTNAME` to `http://127.0.0.1:7402`. Cloud sessions use HTTPS
under `tunnel.outcomeci.com` (staging: `tunnel.staging.outcomeci.com`) once the
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

Slack credentials remain in Slack CLI's own credential store. OutcomeCI adds
only this non-secret reference to `outcome.yml`:

```yaml
connections:
  - ref: slack_local
    provider: slack
    delivery: on_demand
```

To deliver a human interaction through Slack, declare Slack delivery on the
phase hook:

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

Credentials remain in the named environment variable on the host side of the
capability broker. OutcomeCI removes every connection-declared credential from
the agent environment.

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

Then create the first immutable workflow revision for a workspace:

```bash
oci workflow sync outcome.yml \
  --workspace workspace_abc123 \
  --create
```

When that workflow already exists, make the versioning intent explicit:

```bash
oci workflow sync outcome.yml \
  --workspace workspace_abc123 \
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the supported Python versions and the
formatting, lint, test, and package checks used by CI.
