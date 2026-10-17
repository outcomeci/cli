# Feature Specification: Managed media processor calls in the CLI and runner

**Feature Branch**: `feat/media-processor`
**Created**: 2026-10-17
**Status**: Specified; ready for planning
**Input**: Support the platform-managed `media` connector (`uses: media`, operation `media.process`) in workflows, and replace the runner's and compiler's hard-coded Tavily handling with a general managed-provider path: durable connector-call routing and asynchronous (pending/completed) connector-call responses. In v1 the media input is a trigger attachment of the current run; the runner forwards the reference and the api resolves it. Specification only.

## Context

Today the literal provider name `tavily` is special-cased in four places, and each one is a rule that must instead come from the provider's `managed` flag:

- `src/outcomeci/workflow/language.py` (binding): a `tavily` binding may omit `auth`; when it does the connection is compiled with `auth.managed: true`. Every other connector must either take a credential or be keyless.
- `src/outcomeci/workflow/language.py` (grants): `can:` grants on a `tavily` api must be static values, never trigger or step references.
- `src/outcomeci/workflow/compiler.py` (`auth.managed`): a compiled connection may carry `managed: true` only when the connector is `tavily` and there is no credential.
- `src/outcomeci/broker/executor.py` (`execute`): when the connection's connector is `tavily` and a `connector_client` is wired (managed run), the request is sent to the api's `connector-call` with the broker's `request_id` (the proposal fingerprint) and the provider is never contacted; without a `connector_client` a managed binding fails with `integration.managed_unavailable`.

The managed-run plumbing already exists and is reused unchanged in shape: `cloud_runner/main.py` wires `connector_client` to `CloudClient.workflow_connector_call(lease, step=, capability=, request_id=, input=)`, which posts `connector-call` once and replays it at most once on a retryable error so a lost response never becomes a second billable call. The broker journal keeps one `calls[<proposal_sha256>]` entry per call with `status`, `result` and `receipt`, and `artifacts/records.py` writes it verbatim to `calls.json` in the run record.

The runner never holds media bytes. The step names a trigger attachment, the runner forwards that reference inside the `connector-call` input, and the api resolves it against the run's trigger payload server-side. The cli therefore has no upload path, no content-type handling and no object keys.

### Contracts this feature consumes

- **C1 (connectors)**: the `media` provider exposes `media.process` with input `{attachment: string, outputs: [<output_type>, ...], options?: {<output_type>: {...}}}` and output `{media_type, outputs: {<output_type>: <payload>}, provider, model, duration_seconds?, usage: {minutes}}`. `attachment` is the identifier of a trigger attachment of the current run, as the trigger payload exposes it (v1: email trigger attachments). The locked provider contract carries a `media.registry` key (media type -> output types and their payload schemas); the first cell is `audio -> transcript`. The provider has no credential kinds. Providers carry a `managed` flag; `tavily` is also marked managed.
- **C3 (api)**: the `connector-call` response gains `state: "pending" | "completed"`. For `media` the first call answers `{state: "pending", retry_after_seconds: N}` (N defaults to 10). Re-issuing the same `request_id` under the same lease returns the stored response once complete, or pending again. A different `request_id` is a new billable call. A failed job answers `ok: false` with a `failure_category`. The api resolves `attachment` from the run's trigger payload; an unknown identifier, an attachment whose media type is outside the registry, or one over the api's size limit answers `ok: false` with category `input_invalid`. The runner can cancel a pending receipt (same lease, same `request_id`); cancellation, lease expiry or run cancellation while pending stops the task and yields the receipt state `cancelled`.

## User Scenarios & Testing

### User Story 1 - Transcribe an email attachment in a managed run (Priority: P1)

An author binds `transcribe: {uses: media}`, grants a step `media.process` with `outputs: [transcript]`, and the step hands a recording that arrived as an email attachment to the processor and reasons over the transcript.

**Why this priority**: This is the feature. Everything else exists to make this call durable, billable once and explainable.
**Independent Test**: Run a workflow on an email trigger whose payload carries one audio attachment and whose step calls `media.process` on it in a managed run against an api fixture that answers pending twice, then completed.

**Acceptance Scenarios**:
1. Given a `media` binding without `auth`, when the workflow compiles, then the connection is managed and the compiled connection carries no credential (FR-001, FR-002).
2. Given the step proposes `media.process` with `attachment` set to the identifier of the email attachment and `outputs: [transcript]`, when the broker approves it, then the runner sends `connector-call` with the input unchanged and the proposal fingerprint as `request_id`, and never reads attachment bytes or contacts a media provider itself (FR-003, FR-004).
3. Given the api answers `pending` with `retry_after_seconds`, when the runner polls, then it re-issues the identical `request_id` under the same lease, waits at least `retry_after_seconds` between polls, and returns the completed output to the agent as the result of the single call (FR-005).
4. Given the completed response, when the step finishes, then `calls.json` holds one call with the receipt and every poll attempt, and the transcript payload is the call's `result` (FR-009, FR-010).

### User Story 2 - Local runs and compile-time errors are explicit (Priority: P1)

An author validates and runs locally before binding to cloud, and expects the same clear failure the Tavily managed path gives today.

**Why this priority**: Local runs are the portable contract; a managed-only provider must fail before any side effect, not half way through a step.
**Independent Test**: Compile the fixtures below with `oci workflow validate` and run the valid one locally.

**Acceptance Scenarios**:
1. Given `transcribe: {uses: media, auth: secrets.x}`, when validated, then compilation fails naming the field and saying `media` takes no credential (FR-001).
2. Given a grant `media.process: {outputs: trigger.wanted}`, when validated, then compilation fails because managed grants are static (FR-002).
3. Given a grant `media.process: {outputs: [subtitles]}` and a `media.registry` with only `audio -> transcript`, when validated, then compilation fails naming `subtitles` as an output type no media type supports (FR-002).
4. Given a workflow that binds `media` but whose trigger cannot carry attachments (for example a cron trigger), when validated, then validation emits a warning saying no attachment source exists for `media.process`; compilation still succeeds (FR-012).
5. Given a valid workflow run locally with no managed connector client, when the step proposes `media.process`, then the call fails with `integration.managed_unavailable` before any request, and the journal records the denied call (FR-006).
6. Given a `tavily` binding with `auth: secrets.tavily`, when run locally, then it still calls Tavily directly with the author's key, exactly as today (FR-006, FR-011).

### User Story 3 - Long jobs, timeouts, retries and cancellation are safe (Priority: P2)

An operator expects a stalled or cancelled media job to leave an honest run record and never to be billed twice.

**Why this priority**: Media jobs take minutes; the runner's lease, heartbeat and retry machinery all interact with a pending call.
**Independent Test**: Drive the api fixture through: pending beyond the step's timeout; pending then `ok: false`; pending then run cancellation; and a runner crash between the first `connector-call` and its acknowledgement.

**Acceptance Scenarios**:
1. Given the job is still pending when the step's `timeout` elapses, when the runner stops polling, then it cancels the receipt through the api, the call fails with `integration.timeout`, the journal shows every poll, and no new `request_id` is created for the same proposal (FR-005, FR-007, FR-009).
2. Given the api answers `ok: false` with a `failure_category`, when the runner receives it, then the call is `failed` with that category surfaced in the result, and the agent sees the failure as the tool result (FR-007).
3. Given the run is cancelled or the lease expires while pending, when the runner is told to stop or next polls, then the runner cancels the receipt, the call's receipt state is `cancelled`, and the run fails for the cancellation reason, not as a provider error (FR-007).
4. Given the runner is resumed after a crash that happened before the first `connector-call` was acknowledged, when the broker replays the pending call, then it reuses the same `request_id` and no second job is billed (FR-003, FR-005).
5. Given the same step proposes `media.process` twice with different arguments, when both are approved, then they are two proposals, two `request_id`s and two billable jobs, each with its own receipt (FR-003).

### Edge Cases

`attachment` naming an identifier the trigger payload does not expose; the trigger payload carrying several attachments and the agent naming one that is not audio; an attachment the api rejects as too large or outside the registry (`ok: false`, `input_invalid`); `retry_after_seconds` missing, zero or larger than the remaining step budget; `completed` response missing the requested output type; registry in the locked contract older than the api's; `outputs` pinned in the grant but the agent asks for a subset or a superset; heartbeat failure during a long poll; the cancel request itself failing after a timeout (the call is still `integration.timeout`; the cancel failure is journalled); `tavily` with a credential in a managed run (still proxied through the api, which is today's behaviour); a workflow with both an email trigger and a cron trigger (no warning, since one trigger can carry attachments).

## Requirements

### Functional Requirements

- **FR-001 (binding)**: A binding whose provider is `managed` takes no `auth`; `auth` on it is a compile error naming the field. The compiled connection carries `managed: true` and no credential. A managed provider with credential kinds (Tavily) keeps today's choice: `auth` optional, present means the author's key is used, absent means managed. The `tavily` literal is removed from `language.py` and `compiler.py`; both read `contract["managed"]`.
- **FR-002 (grants)**: `can:` grants on a managed api are static values; a trigger or step reference is a compile error. For `media.process` a grant may pin `outputs` as a list of output types; the compiler checks each requested type against the union of all cells in the `media.registry` key of the locked provider contract (media type is unknown until run time). A step proposal whose `outputs` is not a subset of the pinned list is denied by the broker like any other grant mismatch.
- **FR-003 (routing)**: When the connection is managed and a connector client is wired, the executor sends the call through `connector-call` with the broker's proposal fingerprint as `request_id`, and never resolves a credential or opens a connection to the provider. The `tavily` literal check in `executor.py` is replaced by the connection's `managed` flag. A managed call without a `request_id` still fails with `integration.receipt_required`.
- **FR-004 (attachment reference)**: `media.process` input is `{attachment, outputs, options?}`. `attachment` is the identifier of a trigger attachment of the current run, exactly as the trigger payload exposes it (v1: email trigger attachments). The runner validates the proposal against the contract's input schema and forwards it unchanged; it does not read, resolve, upload or inspect the attachment. Resolution, media-type and size checks are the api's; their failures arrive as `ok: false` with `failure_category: input_invalid` (FR-007).
- **FR-005 (polling)**: A `connector-call` answer with `state: "pending"` is not a result. The runner re-issues the identical request under the same lease, waiting `max(retry_after_seconds, 5 s)` and capping the wait at 60 s, until `completed`, until the step's `timeout` passes, or until the run is cancelled. The api's default `retry_after_seconds` is 10. Steps that call `media` have a step-level `timeout` with a default of 30 minutes; when it passes the runner cancels the receipt (FR-007), the call fails with `integration.timeout` and the journal keeps every attempt. Every poll uses the same `request_id`; a new `request_id` is only ever a new proposal. Responses without `state` are treated as `completed` so Tavily and older apis keep working.
- **FR-006 (local)**: With no connector client, a managed connection whose provider has no local path fails with `integration.managed_unavailable` (category `configuration`) before any request, with a message naming the provider and, where one exists, the local alternative (for Tavily, configuring `auth`). A managed provider with a credential configured runs locally against the provider exactly as today.
- **FR-007 (failure and cancellation)**: An `ok: false` completed response carries the api's `failure_category` into the call result and the `integration.failed` event. On step timeout or run cancellation while pending, the runner cancels the receipt through the api (same lease, same `request_id`), which stops the task; the call records receipt state `cancelled` (timeout additionally fails the call with `integration.timeout`), the runner stops polling and reports the run's own failure category, never a provider failure. Lease expiry while pending is cancelled by the api and surfaces the same way on the next poll. A completed response missing a requested output type is `integration.response_invalid`.
- **FR-008 (step timeout)**: Agent and model steps accept a step-level `timeout` (duration). It bounds the step, including any pending managed call. When a step calls a `media` binding and declares no `timeout`, the compiler sets 30 minutes; other steps keep today's behaviour (no bound) unless the author sets one.
- **FR-009 (run record)**: The run record is complete raw data for the call. `calls.json` keeps, per call: the proposal, an `attempts` list with one entry per `connector-call` request (`requested_at`, `state`, `retry_after_seconds`, `http status` or error) including the cancel request when one was made, the final result and the receipt. `policy.json` is unchanged. Journal events `integration.started`, `integration.pending` (one per poll, `info`), `integration.completed` / `integration.failed` and `integration.cancelled` carry the `proposal_sha256`.
- **FR-010 (result shape)**: The agent sees the connector output as the call result: `{media_type, outputs: {...}, provider, model, duration_seconds?, usage: {minutes}}`. `usage.minutes` is surfaced in the step's usage panel next to search credits.
- **FR-011 (compatibility)**: Existing Tavily workflows compile to the same connection and behave the same locally and in managed runs. Workflows that bind `media` fail clearly on runners whose locked provider contract lacks the `media.registry` key (compile error naming the required connectors version) rather than at run time.
- **FR-012 (attachment source check)**: Where the compiler can tell, a workflow that binds `media` and has no trigger that can carry attachments (v1: no email trigger) gets a validation warning naming the binding and saying `media.process` has no attachment source. It is a warning, not an error: the trigger catalogue may grow and the check is best effort.

### Key Entities

- **Managed provider**: a connector whose `managed` flag says the platform executes its calls; the runner only proposes and polls.
- **Media registry**: the `media.registry` key of the locked provider contract, media type -> output types and payload schemas; the compiler's source of truth for `outputs`.
- **Attachment reference**: the `attachment` argument of a `media.process` proposal; an identifier of a trigger attachment of the current run, forwarded by the runner and resolved by the api.
- **Poll attempt**: one `connector-call` request for a `request_id`, recorded in order under the call.
- **Receipt**: the proposal fingerprint, the single `request_id` for the call and its terminal state (`confirmed`, `failed`, `cancelled`).

## Success Criteria

- **SC-001**: No source file under `src/outcomeci` matches the literal `tavily` outside the connectors registry adapter and tests; the four special cases listed in Context are driven by `managed`.
- **SC-002**: In a fixture that answers `pending` N times then `completed`, the api fixture sees exactly N+1 `connector-call` requests with one `request_id`; a crash injected before the first acknowledgement and a resume produce zero extra `request_id`s.
- **SC-003**: Compile fixtures for `auth` on `media`, referenced grant values, unknown output types and a stale connectors package each fail with the named field before any run directory is created; a `media` workflow on a cron-only trigger validates with the attachment-source warning.
- **SC-004**: A local run of a `media` workflow fails with `integration.managed_unavailable` and no network request; a local Tavily run with a key behaves identically to the current release's test suite.
- **SC-005**: `oci dataset export --check` passes on a run whose media call was pending for several polls, and `calls.json` lets a reader reconstruct each poll, any cancel and the final state without the api.
- **SC-006**: A fixture that stays pending past a 1-minute step `timeout` sees exactly one cancel request for the `request_id`, and the run record shows `integration.timeout` with receipt state `cancelled`.

## Assumptions and Boundaries

Specification only; implementation and rollout require a plan. The api owns attachment resolution, job execution, storage, billing and the registry's deployment; the runner never sees provider credentials or media bytes. The CLI does not download processor outputs as files: payloads come back inline in the call result. Streaming, partial results, video and image cells, and surfacing `usage.minutes` in billing views are out of scope.

Deferred slices, not in v1:

- **Slack files**: attachments carried by Slack-originated triggers or converse replies as a `media.process` source.
- **Outcome-directory files**: a `file` reference to an artifact a prior step wrote, which would require the runner to upload bytes to the api (presigned PUT, upload evidence, content-type detection). Rejected for v1 because trigger attachments cover the first use case without adding a second durable receipt to the runner.

## Open Questions

None. Settled here: step-level `timeout` with a 30-minute default for steps that call `media` (FR-008); on timeout or run cancellation the runner cancels the receipt via the api, which stops the task (FR-007); poll floor 5 s, cap 60 s, api default `retry_after_seconds` 10 s (FR-005).
