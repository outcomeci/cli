# Feature Specification: Durable async triggers

**Created**: 2026-09-15
**Status**: Ready
**Input**: Execute version-matched async webhook/email events through the local listener. Email and webhooks share durable async delivery; no synchronous forwarding.

## User Scenarios & Testing

### User Story 1 — Run when ready (Priority: P1)
A user submits a trigger while their runner is offline; the event waits and executes once the matching runner connects.
Acceptance: acknowledged requests survive service restart and a disconnected runner without becoming retry failures.

### User Story 2 — Recover delivery failures (Priority: P2)
A user can trust that failed transport is retried and unrecoverable work is retained for investigation.
Acceptance: duplicate delivery never creates another invocation; exhausted transport reaches a dead-letter queue; started abandoned work does not automatically repeat effects.

### Edge Cases
Publish outage, admission outage, duplicate receipt, changed idempotency payload, deleted workflow, version mismatch, expired leases, unknown schema, read-only members and listener disconnect.

## Requirements
- FR-001: Acknowledge only durable receipt; store an immutable workflow-version reference.
- FR-002: Route email and webhook through one delivery contract.
- FR-003: Protect claims by workspace, workflow, version, principal and execution lease.
- FR-004: Keep credentials and message contents out of transport envelopes.
- FR-005: Retain uncertain/failed execution without blindly retrying side effects.
- FR-006: Reject/remove synchronous forwarding configuration and controls.

### Key Entities
Trigger event, invocation, outbox dispatch, durable inbox, listener, execution lease and dead-letter record.

## Success Criteria
- SC-001: Every acknowledged proof event remains recoverable across publish/consumer restart.
- SC-002: Repeated transport delivery produces at most one started proof invocation.
- SC-003: Offline runner tests produce zero execution retries or dead letters.
- SC-004: Failure tests retain transport and execution failures for investigation.

## Assumptions
Local execution requires connectivity to its API; a tunnel is not part of this feature. Long-running executions use application leases instead of keeping SQS messages invisible for the entire run.
