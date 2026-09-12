# Feature Specification: Local Slack CLI Integration

## Goal

Allow a developer to configure a user-owned Slack app for an OutcomeCI filesystem workflow with one OutcomeCI command while the official Slack CLI owns Slack authentication and installation.

## Requirements

- Add an `oci integration slack` command group with `setup`, `status`, `run`, and `manifest` operations.
- Detect a missing Slack CLI and return an actionable installation error.
- Generate a Slack CLI project beneath `.outcomeci/integrations/slack` with an on-demand API manifest and Slack hook configuration.
- Generate the Slack project metadata directly so setup never asks the user to link an existing app.
- Authenticate through `slack login` only when `slack auth list` reports no usable authorization.
- Validate the generated manifest and install the local app through Slack CLI.
- Add an idempotent `slack_local` connection to `outcome.yml` without persisting Slack tokens.
- Let `status` distinguish missing CLI, missing project, unregistered workflow connection, and ready configuration.
- Use short-lived `slack api` operations; require no listener, webhook, mention, or slash command.
- Never print, copy, or add Slack CLI credentials to OutcomeCI workflow files.
- Deliver pending `before`, `during`, and `after` interactions whose delivery type is `slack` into configured Slack conversations on demand.
- Persist the Slack channel and root thread timestamp against the local interaction so restarts do not duplicate messages.
- Resolve thread replies into approval, rejection, review feedback, or consultation answers through the existing durable interaction protocol.
- Require explicit affirmative or negative language for approval interactions; ambiguous replies must not advance the workflow.
- Keep a Slack thread associated with its outcome after an individual gate resolves so the same conversation can span phases.
- Support Codex and Claude Code by resuming the local agent session associated with the outcome.
- Reuse the agent's local authentication and persist conversation metadata and transcript-derived usage without credentials.
- Allow the model to answer questions from run state and artifacts, while restricting mutations to validated OutcomeCI tools.
- Launch phase-changing actions in detached workers that survive Slack listener restarts and expose durable process/log metadata.
- Discover Slack users, channels, and user groups so a user or coding agent can assign concrete recipients to a human hook.
- Persist hook recipients only as readable usernames, channel names, or user-group handles in `outcome.yml`.
- Keep Slack IDs in private integration runtime state and never include them in agent prompts, command output, workflow files, or outcome artifacts.
- Prevent an executing agent from reading Slack credentials, changing `outcome.yml`, discovering targets, assigning hooks, invoking an undeclared hook, or fabricating a human response.
- Fail closed when the required local OS isolation primitive is unavailable.
- Let a hook block, continue asynchronously, or ask the requester to choose a bounded wait at runtime.
- Expose a deterministic polling command that reads durable interaction state and optionally waits for a bounded duration.
- Preserve late human responses as outcome context even when the workflow was allowed to continue.

## Acceptance Criteria

- Setup produces deterministic project files and an idempotent workflow connection.
- Re-running setup does not duplicate the connection or overwrite a changed manifest unless explicitly requested.
- Unit tests exercise authenticated and unauthenticated subprocess paths without contacting Slack.
- A locally built wheel exposes all commands and can scaffold a temporary OutcomeCI repository.
- A pending Slack human hook is posted exactly once and subsequent replies remain attached to its thread and outcome run.
- Natural questions receive contextual answers and natural approval language advances only when the state machine permits it.
- A user or agent can discover and assign a Slack person, channel, or group by readable name without receiving an opaque Slack ID.
- Codex or Claude Code can request the configured hook, ask how long to wait, and poll for the response without direct Slack credentials.
- Security tests prove the repository policy is read-only, Slack credentials are masked, only the current outcome directory is writable, capabilities reject other runs/hooks, and accepted responses are verified against Slack.

## Out of Scope

- OutcomeCI-hosted OAuth.
- Copying Slack credentials into OutcomeCI storage.
- General-purpose assistance unrelated to the bound outcome.
