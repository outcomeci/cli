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
      humans:
        during:
          - id: clarify_experience
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
humans:
  after:
    - id: confirm_scope
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
