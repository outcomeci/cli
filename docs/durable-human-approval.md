# Durable human approval

## Specification

A discussion returns its plan separately from the decision (`approved`, `rejected`, `undecided`). An explicit terminal response may close discussion only when the exact plan is unchanged. A revised plan cannot be approved in the same turn. Defaults remain compatible with existing `plan`/`status` consumers; opt into `decision` via returns. A plan must not store the approval decision inside its content.

Managed human waits checkpoint the run and exit, releasing both agent and Vault leases after rotated credentials are safely written back. A matching reply/reaction or timeout resumes the same invocation with fresh credentials and the same immutable workflow revision. Finished steps and outbound effects must not replay. Local execution keeps its interactive polling behavior.

## Plan

- Extend conversation turn statuses with rejection and expose a separate decision output.
- Persist absolute wait deadlines and a watcher descriptor; suspend only at an idle boundary after posting durable outbox entries.
- Pause endpoint carries the run checkpoint, interaction and credential writeback. Fresh claims carry resume artifacts checked for hashes, paths and size limits.
- Restore the original run ID and resume runtime steps without retriggering the workflow.
- API polls only scoped, declared services with no agent lease. Existing ECS reconciliation fences old tasks before launching a resumed runner.

## Tasks

- [x] Approval schema, deterministic outcome and regression tests.
- [x] Durable await/conversation checkpoints and no-replay resume tests.
- [x] Cloud pause/resume handshake and credential boundaries.
- [x] Integrate API contract and run full regressions.
- [x] Document migration and safe recovery limitations.

## Acceptance

An unchanged approved plan advances once; rejection advances with rejected outcome and never authorizes build; substantive changes require another approval. A long human wait holds no runner/agent lease, survives restart, expires at the persisted deadline, handles duplicate replies once, and cannot revive a cancelled or completed invocation. Checkpoints never include injected credentials or arbitrary filesystem paths.

## Migration and rollout

Deploy the compatible API pause/claim endpoints before enabling this CLI in managed runners. Deploy the dashboard waiting-state support before the API begins returning `awaiting_input`. The API must use the same CLI compiler revision as the runner; checkpoints refuse a changed compiled workflow.

For existing workflows, remove the mutable approval field from the draft plan schema and its reasoning instructions. Add `decision` to conversation returns and gate downstream effects directly on `discuss.decision == "approved"`. Do not ask a later model step to reinterpret approval. Updating a workflow applies to new invocations; it does not change a pinned in-flight revision.

A failed legacy run cannot be resumed by this checkpoint protocol. Recover it only after validating the saved approved plan and completed effects, using an explicit migration that preserves that audit trail. Do not redeliver the trigger as a substitute: that can repost the plan or repeat effects.

Snapshots are bounded to 500 files, 4 MiB per file and 16 MiB total. An oversized or invalid checkpoint fails closed rather than silently dropping run state. Local CLI human waits continue polling locally.

## Validation

The full CLI suite passed (521 tests), followed by 12 durable-wait tests including two additional reaction cases. A real synthetic CLI checkpoint also passes the API's declaration, revision, digest and restore checks. Regression coverage includes unchanged approval, rejection, revised-plan reapproval, fresh credentials, absolute deadlines and no duplicate draft or plan post after restore.
