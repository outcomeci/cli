# Feature Specification: Cloud Workflow Execution

**Created**: 2026-09-15

## User Scenarios & Testing

### User Story 1 - Run a synced workflow in OutcomeCI Cloud (P1)

A workspace owner can receive an asynchronous trigger and have the exact synced workflow revision run once on OutcomeCI infrastructure without a local listener.

**Acceptance**: A queued email invocation is claimed once, heartbeated, completed, and records durable evidence even across retryable control-plane failures.

### User Story 2 - Store a typed credential from the CLI (P1)

A user can pipe a secret into `oci vault put` while declaring its provider and authentication contract. Output and process arguments never disclose its value.

**Acceptance**: A Slack bearer credential is stored with provider, type, configuration, and workflow grant metadata and the CLI output contains no secret.

## Requirements

- **FR-001**: The runner MUST execute the immutable workflow revision and trigger payload assigned by the control plane.
- **FR-002**: A runner MUST use a fenced lease with heartbeats and idempotent terminal reconciliation.
- **FR-003**: Credential resolution MUST be limited to paths granted to the workflow and MUST remain outside agent prompts, environment, workspace, logs, and results.
- **FR-004**: Typed CLI writes MUST support `api_key`, `auth_header`, `oauth2`, and `oidc` using the existing cloud credential contract.
- **FR-005**: Noninteractive secret input MUST be accepted through stdin.

## Success Criteria

- One admitted invocation produces at most one active cloud task and one terminal result.
- A simulated email-to-Slack workflow produces one approved message and no secret-bearing output.
- Typed Vault CLI tests cover valid and incomplete contracts and secret redaction.
