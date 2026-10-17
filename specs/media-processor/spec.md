# Feature Specification: Managed media processor calls in the CLI and runner

**Feature Branch**: `feat/media-processor`
**Created**: 2026-10-17
**Status**: Specified; ready for planning
**Input**: Support the platform-managed `media` connector (`uses: media`, operation `media.process`) in workflows, and replace the runner's and compiler's hard-coded Tavily handling with a general managed-provider path: durable connector-call routing, asynchronous (pending/completed) connector-call responses, and presigned uploads of run artifacts so a step can hand bytes to the processor. Specification only.

## Context

Today the literal provider name `tavily` is special-cased in four places, and each one is a rule that must instead come from the provider's `managed` flag:

- `src/outcomeci/workflow/language.py` (binding): a `tavily` binding may omit `auth`; when it does the connection is compiled with `auth.managed: true`. Every other connector must either take a credential or be keyless.
- `src/outcomeci/workflow/language.py` (grants): `can:` grants on a `tavily` api must be static values, never trigger or step references.
- `src/outcomeci/workflow/compiler.py` (`auth.managed`): a compiled connection may carry `managed: true` only when the connector is `tavily` and there is no credential.
- `src/outcomeci/broker/executor.py` (`execute`): when the connection's connector is `tavily` and a `connector_client` is wired (managed run), the request is sent to the api's `connector-call` with the broker's `request_id` (the proposal fingerprint) and the provider is never contacted; without a `connector_client` a managed binding fails with `integration.managed_unavailable`.

The managed-run plumbing already exists and is reused unchanged in shape: `cloud_runner/main.py` wires `connector_client` to `CloudClient.workflow_connector_call(lease, step=, capability=, request_id=, input=)`, which posts `connector-call` once and replays it at most once on a retryable error so a lost response never becomes a second billable call. The broker journal keeps one `calls[<proposal_sha256>]` entry per call with `status`, `result` and `receipt`, and `artifacts/records.py` writes it verbatim to `calls.json` in the run record.

### Contracts this feature consumes

- **C1 (connectors)**: the `media` provider exposes `media.process` with input `{object_key, outputs: [<output_type>, ...], options?: {<output_type>: {...}}}` and output `{media_type, outputs: {<output_type>: <payload>}, provider, model, duration_seconds?, usage: {minutes}}`. The package exports a support registry (media type -> output types and their payload schemas); the first cell is `audio -> transcript`. The provider has no credential kinds. Providers carry a `managed` flag; `tavily` is also marked managed.
- **C3 (api)**: the `connector-call` response gains `state: "pending" | "completed"`. For `media` the first call answers `{state: "pending", retry_after_seconds: N}`. Re-issuing the same `request_id` under the same lease returns the stored response once complete, or pending again. A different `request_id` is a new billable call. A failed job answers `ok: false` with a `failure_category`. Cancellation or lease expiry while pending yields the receipt state `cancelled`.
- **C4 (api)**: `POST /internal/workflow-runs/connector-upload` with the lease token, `step`, `filename`, `content_type` and `size_bytes` answers `{object_key, url, headers, expires_at}` for one presigned S3 PUT. Maximum 500 MB; accepted content types are the registry's media types (initially `audio/*`).

## User Scenarios & Testing

### User Story 1 - Transcribe an audio artifact in a managed run (Priority: P1)

An author binds `transcribe: {uses: media}`, grants a step `media.process` with `outputs: [transcript]`, and the step hands a recording produced by an earlier step to the processor and reasons over the transcript.

**Why this priority**: This is the feature. Everything else exists to make this call durable, billable once and explainable.
**Independent Test**: Run a workflow whose first step writes `recording.mp3` into the outcome directory and whose second step calls `media.process` on it in a managed run against an api fixture that answers pending twice, then completed.

**Acceptance Scenarios**:
1. Given a `media` binding without `auth`, when the workflow compiles, then the connection is managed and the compiled connection carries no credential (FR-001, FR-002).
2. Given the step proposes `media.process` for `recording.mp3` with `outputs: [transcript]`, when the broker approves it, then the runner uploads the bytes through one presigned PUT, sends `connector-call` with the resulting `object_key` and the proposal fingerprint as `request_id`, and never contacts a media provider itself (FR-003, FR-004, FR-008).
3. Given the api answers `pending` with `retry_after_seconds`, when the runner polls, then it re-issues the identical `request_id` under the same lease, waits at least `retry_after_seconds` between polls, and returns the completed output to the agent as the result of the single call (FR-005).
4. Given the completed response, when the step finishes, then `calls.json` holds one call with the receipt, the upload evidence and every poll attempt, and the transcript payload is the call's `result` (FR-009, FR-010).

### User Story 2 - Local runs and compile-time errors are explicit (Priority: P1)

An author validates and runs locally before binding to cloud, and expects the same clear failure the Tavily managed path gives today.

**Why this priority**: Local runs are the portable contract; a managed-only provider must fail before any side effect, not half way through a step.
**Independent Test**: Compile the fixtures below with `oci workflow validate` and run the valid one locally.

**Acceptance Scenarios**:
1. Given `transcribe: {uses: media, auth: secrets.x}`, when validated, then compilation fails naming the field and saying `media` takes no credential (FR-001).
2. Given a grant `media.process: {outputs: trigger.wanted}`, when validated, then compilation fails because managed grants are static (FR-002).
3. Given a grant `media.process: {outputs: [subtitles]}` and a registry with only `audio -> transcript`, when validated, then compilation fails naming `subtitles` as an output type no media type supports (FR-002).
4. Given a valid workflow run locally with no managed connector client, when the step proposes `media.process`, then the call fails with `integration.managed_unavailable` before any upload, and the journal records the denied call (FR-006).
5. Given a `tavily` binding with `auth: secrets.tavily`, when run locally, then it still calls Tavily directly with the author's key, exactly as today (FR-006, FR-011).

### User Story 3 - Long jobs, timeouts, retries and cancellation are safe (Priority: P2)

An operator expects a stalled or cancelled media job to leave an honest run record and never to be billed twice.

**Why this priority**: Media jobs take minutes; the runner's lease, heartbeat and retry machinery all interact with a pending call.
**Independent Test**: Drive the api fixture through: pending beyond the step's deadline; pending then `ok: false`; pending then lease expiry; and a runner crash between upload and the first `connector-call`.

**Acceptance Scenarios**:
1. Given the job is still pending when the step's timeout elapses, when the runner stops polling, then the call fails with `integration.timeout`, the journal shows every poll, and no new `request_id` is created for the same proposal (FR-005, FR-009).
2. Given the api answers `ok: false` with a `failure_category`, when the runner receives it, then the call is `failed` with that category surfaced in the result, and the agent sees the failure as the tool result (FR-007).
3. Given the lease expires or the run is cancelled while pending, when the runner next polls or is told to stop, then the call's receipt state is `cancelled` and the run fails for the cancellation reason, not as a provider error (FR-007).
4. Given the runner is resumed after a crash that happened after the upload but before `connector-call` was acknowledged, when the broker replays the pending call, then it reuses the recorded `object_key` and the same `request_id`; the upload is not repeated and no second job is billed (FR-004, FR-008).
5. Given the same step proposes `media.process` twice with different arguments, when both are approved, then they are two proposals, two `request_id`s and two billable jobs, each with its own receipt (FR-003).

### Edge Cases

Artifact path outside the outcome directory or the run's trigger payload; file larger than 500 MB; content type not in the registry (sniffed type disagrees with the extension); presigned URL expired before the PUT finishes; PUT succeeds but the runner never records the `object_key`; `retry_after_seconds` missing, zero or larger than the remaining step budget; `completed` response missing the requested output type; registry on the runner older than the api's; agent proposes `object_key` directly instead of a file; two steps uploading the same bytes; heartbeat failure during a long poll; `outputs` pinned in the grant but the agent asks for a subset or a superset; `tavily` with a credential in a managed run (still proxied through the api, which is today's behaviour); a connector marked managed whose registry entry the runner does not know how to upload for.

## Requirements

### Functional Requirements

- **FR-001 (binding)**: A binding whose provider is `managed` takes no `auth`; `auth` on it is a compile error naming the field. The compiled connection carries `managed: true` and no credential. A managed provider with credential kinds (Tavily) keeps today's choice: `auth` optional, present means the author's key is used, absent means managed. The `tavily` literal is removed from `language.py` and `compiler.py`; both read `contract["managed"]`.
- **FR-002 (grants)**: `can:` grants on a managed api are static values; a trigger or step reference is a compile error. For `media.process` a grant may pin `outputs` as a list of output types; the compiler checks each requested type against the union of all registry cells (media type is unknown until run time). A step proposal whose `outputs` is not a subset of the pinned list is denied by the broker like any other grant mismatch.
- **FR-003 (routing)**: When the connection is managed and a connector client is wired, the executor sends the call through `connector-call` with the broker's proposal fingerprint as `request_id`, and never resolves a credential or opens a connection to the provider. The `tavily` literal check in `executor.py` is replaced by the connection's `managed` flag. A managed call without a `request_id` still fails with `integration.receipt_required`.
- **FR-004 (upload)**: Before sending `media.process`, the runner turns the step's `file` reference into an `object_key`: it requests one presigned PUT from `connector-upload` (lease token, step, filename, content type, size), PUTs the bytes, and records `{object_key, content_sha256, size_bytes, content_type, expires_at}` on the journal call before the first `connector-call`. A replay of the same proposal with a recorded `object_key` reuses it. Upload failures fail the call with `integration.upload_failed` and the api's category when it gave one. Size over 500 MB or a content type outside the registry fail before any request (`integration.input_invalid`).
- **FR-005 (polling)**: A `connector-call` answer with `state: "pending"` is not a result. The runner re-issues the identical request under the same lease, waiting `max(retry_after_seconds, floor)` and backing off up to a cap, until `completed`, until the step's deadline passes, or until the run is cancelled. Polling is bounded by the step's `timeout_seconds`; when the deadline passes the call fails with `integration.timeout` and the journal keeps every attempt. Every poll uses the same `request_id`; a new `request_id` is only ever a new proposal. Responses without `state` are treated as `completed` so Tavily and older apis keep working.
- **FR-006 (local)**: With no connector client, a managed connection whose provider has no local path fails with `integration.managed_unavailable` (category `configuration`) before any upload or request, with a message naming the provider and, where one exists, the local alternative (for Tavily, configuring `auth`). A managed provider with a credential configured runs locally against the provider exactly as today.
- **FR-007 (failure and cancellation)**: An `ok: false` completed response carries the api's `failure_category` into the call result and the `integration.failed` event. Cancellation or lease expiry while pending records receipt state `cancelled` on the call; the runner stops polling and reports the run's own failure category, never a provider failure. A completed response missing a requested output type is `integration.response_invalid`.
- **FR-008 (file reference)**: `media.process` is proposed with `file` in place of `object_key`. `file` is a path relative to the run's outcome directory (a prior step's declared artifact or returned file) or `trigger.<field>` naming a trigger payload file. The runner resolves it under the outcome root (escapes are `integration.request_denied`), uploads it (FR-004), and sends the api `{object_key, outputs, options}` with `file` removed. A proposal that names `object_key` directly is denied: object keys are minted by the api for this run only.
- **FR-009 (run record)**: The run record is complete raw data for the call. `calls.json` keeps, per call: the proposal, the upload evidence, an `attempts` list with one entry per `connector-call` request (`requested_at`, `state`, `retry_after_seconds`, `http status` or error), the final result and the receipt. `policy.json` is unchanged. Journal events `integration.started`, `integration.pending` (one per poll, `info`), `integration.completed` / `integration.failed` and `integration.cancelled` carry the `proposal_sha256`.
- **FR-010 (result shape)**: The agent sees the connector output as the call result: `{media_type, outputs: {...}, provider, model, duration_seconds?, usage: {minutes}}`. `usage.minutes` is surfaced in the step's usage panel next to search credits.
- **FR-011 (compatibility)**: Existing Tavily workflows compile to the same connection and behave the same locally and in managed runs. Workflows that bind `media` fail clearly on runners whose connectors package lacks the registry (compile error naming the required connectors version) rather than at run time.

### Key Entities

- **Managed provider**: a connector whose `managed` flag says the platform executes its calls; the runner only proposes, uploads and polls.
- **Support registry**: connectors export of media type -> output types and payload schemas; the compiler's source of truth for `outputs` and content types.
- **File reference**: the `file` argument of a `media.process` proposal; resolved and replaced by an `object_key` before the call leaves the runner.
- **Upload evidence**: `{object_key, content_sha256, size_bytes, content_type, expires_at}` recorded on the journal call.
- **Poll attempt**: one `connector-call` request for a `request_id`, recorded in order under the call.
- **Receipt**: the proposal fingerprint, the single `request_id` for the call and its terminal state (`confirmed`, `failed`, `cancelled`).

## Success Criteria

- **SC-001**: No source file under `src/outcomeci` matches the literal `tavily` outside the connectors registry adapter and tests; the four special cases listed in Context are driven by `managed`.
- **SC-002**: In a fixture that answers `pending` N times then `completed`, the api fixture sees exactly N+1 `connector-call` requests with one `request_id` and exactly one presigned PUT; a crash injected after the PUT and resume produces zero extra PUTs and zero extra `request_id`s.
- **SC-003**: Compile fixtures for `auth` on `media`, referenced grant values, unknown output types and a stale connectors package each fail with the named field before any run directory is created.
- **SC-004**: A local run of a `media` workflow fails with `integration.managed_unavailable` and no network request; a local Tavily run with a key behaves identically to the current release's test suite.
- **SC-005**: `oci dataset export --check` passes on a run whose media call was pending for several polls, and `calls.json` lets a reader reconstruct the upload, each poll and the final state without the api.

## Assumptions and Boundaries

Specification only; implementation and rollout require a plan. The api owns job execution, storage, billing and the registry's deployment; the runner never sees provider credentials. The CLI does not download processor outputs as files: payloads come back inline in the call result. Streaming, partial results, video and image cells, direct `object_key` reuse across runs, retention of uploaded objects, and surfacing `usage.minutes` in billing views are out of scope. Chunking files larger than 500 MB is out of scope. The `file`-reference design in FR-008 is the simplest shape that keeps authorization in the broker and makes replays free; alternatives (an explicit `upload` step, or the agent uploading through a tool) were rejected as adding a second durable receipt.

## Open Questions

- **[NEEDS CLARIFICATION] Trigger payload files (api)**: `trigger.<field>` as a `file` source assumes the runner can read trigger payload bytes. Today trigger payloads are JSON and email attachments are stored metadata whose retrieval "requires separate authorization". Either the claim must carry attachment bytes or a retrievable reference, or FR-008 ships with outcome-directory paths only. The latter is the proposed first slice.
- **[NEEDS CLARIFICATION] Step timeout (cli)**: agent and model steps have no `timeout` today; only `await` and `converse` do. FR-005 needs a bound. Proposal: a step-level `timeout` on agent and model steps (default 1h) that caps polling; absent an agreed default, a `media` default of 30 minutes per call.
- **[NEEDS CLARIFICATION] Input schema vs. the wire shape (connectors)**: the registry's `media.process` input names `object_key`. The runner validates the agent's proposal, which carries `file`, before substitution. Either the connectors contract lists `file` as an input the api ignores, or the cli validates against a derived schema. Proposal: the contract names `file` and marks `object_key` as runner-filled; the api receives only `object_key`.
- **[NEEDS CLARIFICATION] Content type detection**: whether the runner sniffs bytes or trusts the artifact extension; the api rejects mismatches, so a sniff before upload is cheaper. Proposal: extension first, sniff when the extension is unknown.
- **Poll floor and cap**: proposed 5 s floor, 60 s cap, honouring `retry_after_seconds` when it is larger; to be confirmed against the api's job latency.
