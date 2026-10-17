# Implementation Plan: Managed media processor calls in the CLI and runner

**Branch**: `feat/media-processor` | **Date**: 2026-10-17 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/media-processor/spec.md`

## Summary

Replace the four `tavily` literals with the provider contract's `managed` flag,
teach the broker that a `connector-call` answer can be `pending`, and bound
that polling with a new step-level `timeout`. The compiled connection gains a
connection-level `managed: bool` (the provider is platform-managed) next to
the existing `auth.managed` (the platform supplies the credential), and
`base_url` may be `null` on a managed connection, which is what makes the
executor route the call through `connector_client` instead of HTTP. The
`PolicyExecutor` owns the poll loop because it owns the journal: one call,
one `request_id` (the proposal fingerprint), an `attempts` list with one entry
per `connector-call` or cancel request, and `integration.pending` /
`integration.cancelled` events. `CloudClient` gains `workflow_connector_cancel`;
`ExecutionOptions` gains `connector_cancel` and `run_cancelled` so the runner
can cancel a receipt on timeout or when its heartbeat reports the run gone.
Compile-time rules for `media` (no `auth`, static grants, known output types,
attachment-source warning) read `contract["managed"]` and
`contract["media"]["registry"]` from `outcomeci-connectors>=0.15.0`.

## Technical Context

**Language/Version**: Python 3.11+ (CI runs 3.12)

**Primary Dependencies**: `httpx`, `jsonschema`, `PyYAML`,
`outcomeci-connectors` (pin moves from `>=0.14.0,<0.15` to `>=0.15.0,<0.16`,
the release carrying `managed` and `media.registry`)

**Storage**: the per-run broker journal (`.outcomeci/.broker/<run_id>/journal.json`)
and the run-directory records it feeds (`calls.json`, `policy.json`, `effects.json`)

**Testing**: `pytest -q`, `ruff format --check src tests`, `ruff check src tests`
(CONTRIBUTING.md; line length 100)

**Target Platform**: the `oci` CLI on a developer machine (local runs, `oci
workflow validate`) and the managed runner image (`Dockerfile.runner`, installs
`outcomeci-connectors` from PyPI)

**Project Type**: single Python package (`src/outcomeci`) with a `tests/` tree

**Performance Goals**: a pending media call costs the runner one HTTP request
per poll at 5 s to 60 s intervals for up to the step `timeout` (default 30 min);
the heartbeat thread keeps the lease alive independently

**Constraints**: one billable job per proposal (the `request_id` never changes
for a proposal, across polls and across a crash resume); no credential, no
media bytes and no provider endpoint in the runner for a managed call; no
`tavily` literal left under `src/outcomeci` (SC-001); older apis that answer
without `state` keep working

**Scale/Scope**: nine source files modified, one created
(`src/outcomeci/broker/pending.py`), one test module created
(`tests/test_media.py`), three test modules extended

## Constitution Check

The repo has no constitution file. The gates below are the ones the spec and
the upstream plans impose; all pass by design.

| Gate | Status |
|------|--------|
| No consumer names a provider: every rule keys on `managed`, `base_url_from`, `media.registry` | PASS (Phase 1 D1, D2; SC-001 enforced by a test that greps `src/outcomeci` for `tavily`) |
| One proposal is one `request_id`; a lost response or a resume never creates a second job | PASS (D3, D4: the fingerprint is the `request_id`; a journal call in `pending` is resumed, never re-proposed) |
| The run record is complete raw data (`calls.json` reconstructs every poll and cancel without the api) | PASS (D5) |
| Existing Tavily workflows compile to the same connection shape plus `managed: true` and behave the same locally and managed | PASS (D1 keeps `auth.managed` semantics; D2 keeps the proxy-when-wired rule) |
| Fail at compile time, not run time, when the platform cannot serve the binding | PASS (D6: contract without `managed` or without `media.registry` is a `ConfigError`) |

## Project Structure

### Documentation (this feature)

```text
specs/media-processor/
├── spec.md              # Feature specification (authoritative)
└── plan.md              # This file; research and contracts are inlined below
```

No `tasks.md` is produced here; the repo has no Spec Kit install.

### Source Code (repository root)

```text
src/outcomeci/
├── workflow/
│   ├── language.py          # MODIFY: _apis reads contract["managed"], emits connection.managed,
│   │                        #   base_url may be None; _grants static rule + outputs registry check;
│   │                        #   STEP_FIELDS["agent"] += "timeout"; lower() warning + default timeout
│   └── compiler.py          # MODIFY: _http_auth drops the connector literal; connection.managed
│                            #   validated; base_url null only when managed; validation_warnings
├── broker/
│   ├── executor.py          # MODIFY: dispatch on connection["managed"]; raw connector-call
│   │                        #   response (state normalised); managed_unavailable message
│   ├── policy.py            # MODIFY: pending resume, poll loop call-out, attempts, new events,
│   │                        #   failed/cancelled statuses, deadline + stop + cancel wiring
│   ├── pending.py           # CREATE: poll(...) pure loop: backoff, deadline, stop, cancel, outcomes
│   └── server.py            # MODIFY: Broker/serve take connector_cancel, run_cancelled, deadline
├── runtime/
│   └── engine.py            # MODIFY: ExecutionOptions.connector_cancel/run_cancelled; step
│                            #   deadline from block["timeout_seconds"]; invoke timeout from it
├── cloud_runner/
│   ├── client.py            # MODIFY: workflow_connector_cancel; connector-call 404 handling
│   └── main.py              # MODIFY: wire cancel + run_cancelled (heartbeat_failure) into options
├── artifacts/
│   └── records.py           # UNCHANGED (calls.json is the journal verbatim; attempts ride along)
└── cli.py                   # MODIFY: `validate` prints validation_warnings

pyproject.toml               # MODIFY: outcomeci-connectors>=0.15.0,<0.16
Dockerfile.runner            # UNCHANGED (installs from PyPI; the pin above moves it)
README.md                    # MODIFY: media binding, step timeout, managed providers, digest note

tests/
├── test_media.py            # CREATE: compile rules, dispatch, polling, cancel, journal shape
├── test_tavily.py           # MODIFY: message text, managed flag assertions, no-state compat
├── test_custom_connector.py # MODIFY: connection.managed is False, base_url_from still honoured
├── test_run_records.py      # MODIFY: calls.json carries attempts on managed calls
└── test_cloud_runner_flow.py # MODIFY: execution_options wires connector_cancel + run_cancelled
```

**Structure Decision**: the existing single-package layout. The only new
module is `broker/pending.py`, a pure function with injectable clock and
sleep, so the poll loop is unit-testable without threads, sockets or a
runner; `policy.py` calls it and does the journal writes around it.

## Phase 0: Research

Findings from reading the named files and the upstream plans
(`connectors/specs/media-provider/plan.md`,
`api/specs/222-media-processor/{plan,contracts/connector-call}.md`).

- **R1. Where the literals live and what each one means.**
  `language.py:585` (auth may be omitted for tavily), `language.py:593`
  (omitted auth becomes `auth.managed: true`), `language.py:334` (tavily
  grants must be static), `compiler.py:306` (`auth.managed` only for keyless
  tavily), `executor.py:793-809` (proxy when `connector_client` is wired,
  else `managed_unavailable` when `auth.managed`). Note the executor proxies
  tavily **whether or not** a credential is bound: a managed run with
  `auth: secrets.tavily` still goes through the api. That is the "provider is
  managed" fact, distinct from "the platform supplies the credential"
  (`auth.managed`). Both facts must survive, so the compiled connection needs
  a connection-level `managed` in addition to `auth.managed` (D1).

- **R2. The connectors contract after 0.15.0.** `managed` is emitted on every
  contract (`False` by default, `True` on `tavily` and `media`).
  `base_url_from: "workflow"` is emitted only when `base_url is None and not
  managed`, so `custom` keeps it and `media` has `base_url: null` with no
  `base_url_from`. `media` accepts only `{"kind": "none"}`, has
  `max_requests: 10`, one operation `process` (`POST /process`, body
  `{{ input }}`, `side_effect: "execute"`, grantable `outputs` only) and a
  top-level `media: {registry: {audio: {transcript: {...}}}}`. The `request`
  of a managed, origin-less provider is not an HTTP call the broker makes.

- **R3. The fingerprint already is the request_id, and `execute` keeps it
  stable.** `policy.py:470-480` digests `{revision, step, capability, request
  minus purpose}` and adds a random `read_id` only when `_reads_only` holds
  (`side_effect == "read"`, which Tavily search is). `media.process` is
  `execute`, so the same proposal in the same revision digests to the same
  `request_id` on a crash resume (US3.4) and two different argument sets are
  two proposals (US3.5). No change to `digest` is needed. Caveat: `revision`
  is the compiled hash, which moves with the runner's connectors build; a
  resume on a different runner build after a crash mid-poll would re-digest.
  D4 handles that by matching a pending managed call in the journal by
  `(step, capability, request)` before fingerprinting a fresh proposal.

- **R4. Today's journal refuses to resume a `pending` call.** `policy.py:482-493`
  returns a `confirmed` call's result as `replayed` and raises
  `integration.effect_not_replayable` for any other non-`unsent` status,
  including `pending`. For a managed call `pending` means "the api holds a
  receipt for this `request_id`", which is exactly the state a resume must
  poll, not refuse. D4 carves that case out for managed connections only.

- **R5. Where the agent sees failures.** For a connector-client result the
  executor returns the api's dict as-is; `policy.py:642-666` stores it under
  `status: confirmed`, derives `ok` and emits `integration.completed` or
  `integration.failed`, and returns the dict to the agent as the tool result.
  `ok: false` completed receipts therefore already reach the agent as data
  (FR-007, US3.2); the plan adds `failure_category` and `output.error` to the
  event and sets the call status to `failed` so `effects.json` and
  `calls.json` say so. Timeout and cancellation are the two cases that must
  raise (`integration.timeout`, `integration.cancelled`) so the step stops.

- **R6. Run cancellation reaches the runner only through the heartbeat.**
  `cloud_runner/main.py` has no cancel signal; `pulse()` appends to
  `heartbeat_failure` when the api refuses a heartbeat (lease lost, run
  cancelled) and the main loop raises on the next step boundary. A poll loop
  inside a step must read that list itself, which is what
  `ExecutionOptions.run_cancelled` is for (D7).

- **R7. The step bound today.** Agent steps run under `invoke(..., 7200, ...)`
  (`engine.py:768`), a process timeout. Model steps have no bound. Neither
  reaches the broker. `await` and `converse` already lower a `timeout` field
  to `timeout_seconds` through `duration_seconds`, so the agent-step
  `timeout` reuses that parser and vocabulary (D8).

- **R8. Journal lock scope.** `PolicyExecutor.execute` holds the per-run
  `flock` across the whole call, HTTP included. The engine runs steps and
  `for_each` items sequentially and the model-step tool loop is sequential,
  so holding it across a 30-minute poll blocks nothing today. The loop still
  calls `_save` after every attempt so a crash leaves the latest attempt on
  disk (and in the checkpoint, which copies `journal.json`).

- **R9. Warnings have no channel.** `compile_workflow` returns
  `capability_warnings` (model capability reports) and `oci validate` prints
  them; there is no general validation-warning list. D9 adds
  `validation_warnings` next to it rather than overloading the model list.

- **R10. Grant matching is equality.** `same()` compares lists by equality, so
  `outputs: [transcript]` pinned in a grant admits exactly `[transcript]`.
  FR-002 says "not a subset is denied"; a strict subset would also be denied
  today. Subset matching for list arguments would loosen Tavily's
  `include_domains` grants (a test asserts `[]` is outside `["docs.python.org"]`),
  so v1 keeps equality. Deviation recorded in the Deviations section.

- **R11. `usage.minutes` surfacing.** The cli never reads `credits` anywhere;
  the api derives usage from receipts. The cli's job for FR-010 is to keep
  `usage.minutes` in the stored result and to carry it on the
  `integration.completed` / `integration.failed` event (`minutes=`), as data
  for the api and www usage panel. No panel code in this repo.

- **R12. api behaviour the poller relies on** (connector-call contract):
  `state` always present; `retry_after_seconds` only when pending (fixed 10);
  `ok/status/output` only when completed or cancelled; `audit` always present;
  same `request_id` with a different input digest is HTTP 409 (reaches the cli
  as `CoreError("core_conflict", retryable)`, retried once by
  `workflow_connector_call`, then raised); cancel returns
  `{state, request_id}`, 404 unknown, 409 synchronous provider; a job older
  than 2 h is failed by the api with `timeout`; run cancel and lease expiry
  cancel the job server-side and the next poll sees `state: "cancelled"`.

## Phase 1: Design

### D1. Compiled connection shape

`language.py::_apis` reads the contract once and emits:

```python
managed = contract.get("managed")
if not isinstance(managed, bool):
    raise ConfigError(f"{field}: {found.name} contract predates managed providers; "
                      "install outcomeci-connectors>=0.15.0")
workflow_origin = contract.get("base_url_from") == "workflow"   # unchanged
base_url = _workflow_origin(...) if workflow_origin else contract["base_url"]  # may be None
keyless = [item["kind"] for item in accepts] == ["none"]
if auth is None and not keyless and not managed:
    raise ConfigError(f"{field}.auth is required, such as secrets.{name}")
if auth is not None and keyless:
    raise ConfigError(f"{field}.auth is not used: {found.name} takes no credential")
connection_auth = {"connector": found.name, "accepts": accepts}
if auth is not None: connection_auth["credential"] = secrets[secret]
elif managed: connection_auth["managed"] = True
connections.append({"ref": name, "provider": "http", "base_url": base_url,
                    "managed": managed, "auth": connection_auth, ...})
```

Semantics, stated in the `_apis` docstring:

- `connection.managed`: the provider is platform-managed; in a managed run
  every call goes through `connector_client`.
- `connection.auth.managed`: no credential is bound; the platform's credential
  (or no credential at all, for `media`) is used. Only present when
  `connection.managed` is true.
- `connection.base_url is None`: the provider has no origin the runner may
  call; local runs cannot serve it. Only legal when `connection.managed`.

For Tavily this produces today's connection plus `managed: true`
(FR-011). For `media` it produces `{base_url: None, managed: True, auth:
{connector: "media", accepts: [{kind: none, ...}], managed: True}}`. `custom`
is `managed: False` and keeps `base_url_from` handling unchanged.

`compiler.py::validate_lowered` (connections loop) and `_http_auth`:

- `item["managed"]` must be a bool (default `False` for lowered documents
  that omit it, so hand-written v2 documents keep validating).
- `base_url` passes through `_http_origin` unless it is `None`; `None` is a
  `ConfigError` unless `managed` is true.
- `_http_auth(value, field, *, managed: bool)`: `auth.managed` is a
  `ConfigError` when `not managed` or when `credential` is present (message:
  `"{field}: managed auth takes no credential"` / `"{field}: managed auth
  requires a managed connector"`). The `connector != "tavily"` test and the
  Tavily wording go away.
- `executor.doctor` skips the connectivity check for `base_url is None`
  connections and reports `credential_reference` as `managed` instead of
  `pass`/`fail`.

### D2. Executor dispatch

`executor.py::execute`, replacing lines 793-809:

```python
if connection.get("managed"):
    if self.connector_client is not None:
        if not request_id:
            raise IntegrationError("integration.receipt_required",
                "managed calls require a durable receipt", category="configuration")
        response = self.connector_client(step=step, capability=capability,
                                         request_id=request_id, input=dict(inputs))
        return _managed_response(response)
    if connection["base_url"] is None or connection["auth"].get("managed"):
        raise IntegrationError("integration.managed_unavailable",
            _managed_unavailable_message(connection), category="configuration")
# else: the author bound a credential to a managed provider with an origin
# (Tavily BYOK): fall through to the HTTP path exactly as today.
```

`_managed_unavailable_message` names the provider and, when
`auth.accepts` lists any kind other than `none`, adds "configure `auth` for a
local run"; for `media` it says the provider runs only in managed runs.

`_managed_response` normalises: `state = response.get("state", "completed")`
(FR-005 compatibility); a `completed`/`cancelled` response must carry `ok`
and `status`, otherwise `integration.response_invalid`
(category `validation`). The executor does **not** loop: one call to
`execute` is one `connector-call` request, so the journal can record each.
Everything that returns to the agent still passes through `_project`-free
pass-through as today (the api already projects).

Output-type check (FR-007 last sentence): for a completed `ok: true`
response on an operation whose contract has `media`, every entry of
`inputs["outputs"]` must be a key of `response["output"]["outputs"]`, else
`integration.response_invalid` naming the missing type. Done in the executor
because it has the inputs and the contract; the policy layer records the
raised error as a `failed` call.

### D3. Poll loop: `broker/pending.py`

```python
POLL_FLOOR_SECONDS = 5
POLL_CAP_SECONDS = 60
DEFAULT_RETRY_AFTER_SECONDS = 10

@dataclass
class Attempt:        # one journal entry
    kind: str         # "call" | "cancel"
    requested_at: str # UTC ISO
    state: str | None # pending | completed | cancelled | None (error)
    retry_after_seconds: int | None
    http_status: int | None
    error: str | None
    duration_ms: int

def poll(first, *, call, cancel, deadline, stop, record, clock=time.monotonic,
         sleep=time.sleep) -> tuple[str, dict]:
    """Drive one managed call from its first response to a terminal state.
    Returns (outcome, response): outcome in {"completed", "cancelled",
    "timeout", "run_cancelled"}. `record(Attempt)` is called after every
    request so the caller can journal it under its own lock."""
```

Rules (FR-005, FR-007):

- `wait = min(max(retry_after_seconds or 10, 5), 60)`; also clipped to the
  remaining budget so the deadline is never overshot by more than one floor.
- Before each sleep and each request: if `stop()` is true, call `cancel`
  once, record the attempt, return `run_cancelled`. If `clock() >= deadline`,
  call `cancel` once, record it, return `timeout`.
- A `CoreError` from `call` that is retryable is one more attempt with
  `error` set and the same backoff (the lease heartbeat keeps the receipt
  alive); a non-retryable `CoreError` propagates (the policy layer marks the
  call `uncertain`, as for any transport failure today).
- `cancel` failures (404, 409, transport) are recorded as an attempt with
  `error` and do not change the outcome: a timeout is still
  `integration.timeout` (edge case in the spec).
- `state: "cancelled"` from the api returns `cancelled` with that response.

The function never touches the journal, the lock, threads or the api
client; tests drive it with a fake clock and a scripted `call`.

### D4. Policy layer: resume, attempts, statuses, events

`policy.py::execute` changes, all inside the existing lock:

1. **Resume a pending managed call.** Before computing `previous`, find any
   call in `state["calls"]` with `status == "pending"`, `managed: true`,
   same `step`, `capability` and `request` (purpose excluded). If found, use
   its `proposal_sha256` as the fingerprint and skip proposal, review and
   `integration.started`; emit `integration.pending` with `resumed: true`
   and go straight to step 4 with `first = executor.execute(...)`. Only
   managed calls take this path; any other `pending` call still raises
   `effect_not_replayable` (R4).
2. **Mark the call managed.** When the connection is managed and a
   `connector_client` is wired, `call["managed"] = True` and
   `call["attempts"] = []`. Unmanaged calls carry neither key, so
   `calls.json` for a Tavily BYOK local run is byte-identical to today.
3. **First request.** `result = executor.execute(..., request_id=fingerprint)`;
   if `call["managed"]`, append the attempt (`kind: "call"`) and `_save`.
4. **Pending.** If `result["state"] == "pending"`: emit
   `integration.pending` (`info`, `retry_after_seconds=`) and hand over to
   `pending.poll` with `call=` a closure that re-invokes
   `executor.execute` with the same fingerprint, `cancel=` the
   `connector_cancel` callable bound to the fingerprint, `deadline=` the
   step deadline, `stop=` the `run_cancelled` predicate, and `record=` a
   closure that appends the attempt, emits `integration.pending` for each
   pending answer, and `_save`s.
5. **Terminal handling** (`outcome, response`):
   - `completed`, `ok: true`: status `confirmed`, `result = response` plus
     `receipt`; `integration.completed` with `minutes=` from
     `output.usage.minutes` when present. Returned to the agent.
   - `completed`, `ok: false`: status `failed`, `result = response`;
     `integration.failed` (`error`) with `failure_category=` and
     `error=response["output"]["error"]`. Returned to the agent as the tool
     result (US3.2). The pre-launch rejections (`integration.attachment_not_found`,
     `integration.media_unsupported`, `integration.output_unsupported`,
     `integration.options_unused`, all `failure_category: input_invalid`)
     and `integration.failed` arrive this way and are not re-mapped: the
     agent sees the api's `output.error` code and `detail` verbatim.
   - `cancelled` (the api cancelled it: run cancel, lease expiry):
     status `cancelled`, `receipt_state: "cancelled"`, `result = response`;
     `integration.cancelled` (`warning`, `reason="api"`); raise
     `IntegrationError("integration.cancelled", category="cancelled")`.
   - `run_cancelled` (the runner noticed first): same as above with
     `reason="run_cancelled"`, result `{ok: false, status: 409, output:
     {error: "integration.cancelled"}}`.
   - `timeout`: status `cancelled`, `receipt_state: "cancelled"`, result
     `{ok: false, error: "integration.timeout", timeout_seconds: N}`;
     `integration.cancelled` (`reason="timeout"`) then raise
     `IntegrationError("integration.timeout", category="timeout")`. The
     journal keeps every attempt including the cancel (SC-006).

`_call_succeeded` in `engine.py` (effects.json) already reads `result.ok`;
the new `failed`/`cancelled` statuses need no change there.

Event vocabulary added: `integration.pending`, `integration.cancelled`. Both
carry `proposal_sha256`, `step`, `capability`; `integration.pending` carries
`retry_after_seconds` and `attempt` (1-based); `integration.cancelled`
carries `reason` in `{"timeout", "run_cancelled", "api"}`. All flow through
`event_sink` to the api heartbeat as every journal event does today (one
heartbeat POST per poll; at the 10 s default that is at most 180 over a
30-minute step).

### D5. Journal and `calls.json` shape

`records.py` is unchanged: it writes the journal's `calls` verbatim, so each
managed call gains these keys with no record-schema bump
(`outcomeci.calls/v1` keeps its shape; new optional keys on a call):

```json
{
  "capability": "transcribe.process", "step": "notes", "status": "confirmed",
  "proposal_sha256": "<64 hex>", "request": {"attachment": "att_01", "outputs": ["transcript"]},
  "managed": true,
  "attempts": [
    {"kind": "call", "requested_at": "...", "state": "pending", "retry_after_seconds": 10,
     "http_status": 200, "error": null, "duration_ms": 120},
    {"kind": "call", "requested_at": "...", "state": "pending", "retry_after_seconds": 10, ...},
    {"kind": "call", "requested_at": "...", "state": "completed", "retry_after_seconds": null, ...}
  ],
  "result": {"state": "completed", "ok": true, "status": 200, "output": {...}, "audit": {...},
             "receipt": "<64 hex>"}
}
```

A cancelled call adds `"receipt_state": "cancelled"` and an attempt with
`kind: "cancel"`. `policy.json` is unchanged (FR-009). `oci dataset export
--check` reads `calls.json` as data and passes unchanged (SC-005); a test
asserts a run with three attempts round-trips through `calls_record`.

### D6. Compile rules for managed providers and `media`

In `language.py`:

- `_grants`: `if reference is not None and apis[api]["contract"]["managed"]:
  raise ConfigError(f"{entry_field}.{name}: grants on a managed api must be
  static values")`. The Tavily wording goes; `tests/test_tavily.py` updates
  its `match`.
- `_grants`, after the literal is accepted: when the contract has a `media`
  key and `name == "outputs"`, the literal must be a non-empty list of
  strings each in `union = {t for cell in registry.values() for t in cell}`;
  otherwise `ConfigError(f"{entry_field}.outputs: {bad!r} is not an output
  type any media type supports (registry offers: {sorted(union)})")`
  (US2.3). `options` is not grantable, so nothing to check there.
- `_apis`: the `managed` type check from D1 doubles as the "stale connectors"
  error (FR-011). A `media`-shaped provider (contract has `media`) whose
  registry is missing or empty is a `ConfigError` naming
  `outcomeci-connectors>=0.15.0`.
- `lower()`: after `_trigger`, `attachment_sources = {"email"}`; for each
  api whose contract has `media`, if `trigger_name not in attachment_sources`,
  append `f"apis.{name}: media.process has no attachment source; the
  {trigger_name} trigger carries no attachments"` to `warnings`, returned in
  `_v1["warnings"]` and copied to `graph["warnings"]` by `load()`. A
  dispatcher trigger is treated like cron (no attachments known).

In `compiler.py::_compile`: `compiled["validation_warnings"] =
graph.get("warnings", [])`; the revision input excludes it. `cli.py`
`validate` prints `validation_warnings` next to `capability_warnings`.

### D7. Runner wiring: cancel and run-cancelled

`cloud_runner/client.py`:

```python
def workflow_connector_cancel(self, lease_token: str, *, request_id: str) -> dict[str, Any]:
    return self._post("connector-call/cancel",
                      {"lease_token": lease_token, "request_id": request_id}, timeout=30)
```

404 and 409 arrive as `CoreError("core_rejected")` / `CoreError("core_conflict")`
through `_post`; the poller records them as a cancel attempt with `error`
and moves on (D3). No replay: a cancel is idempotent on the api side but a
second request buys nothing.

`runtime/engine.py::ExecutionOptions` gains
`connector_cancel: Callable[..., dict] | None = None` and
`run_cancelled: Callable[[], bool] | None = None`. `serve_capability` /
`Broker` accept `connector_cancel`, `run_cancelled`, `deadline` and pass
them to `PolicyExecutor(..., connector_cancel=, run_cancelled=, deadline=)`.
Without a `connector_cancel` (local runs) the poller never cancels, which
cannot occur because local runs have no `connector_client` either.

`cloud_runner/main.py::execution_options`:

```python
connector_cancel=lambda **kwargs: client.workflow_connector_cancel(lease, **kwargs),
run_cancelled=lambda: bool(heartbeat_failure),
```

`workflow_failure_category` maps an `IntegrationError` whose code is
`integration.cancelled` to `agent_cancelled` (already a known category) and
`integration.timeout` to `workflow_execution_failed` with the error detail,
so the run reports its own reason, not a provider failure (FR-007).

### D8. Step-level `timeout`

`language.py`: `STEP_FIELDS["agent"]` gains `"timeout"`. `_agent_step`
lowers it with `duration_seconds(step["timeout"], f"{field}.timeout")` into
`block["timeout_seconds"]`. When absent and any grant's api has a contract
with `media`, `block["timeout_seconds"] = DEFAULT_MEDIA_STEP_TIMEOUT_SECONDS
= 1800` (FR-008). Model steps are agent steps with `using`, so the same
field covers both. `await`/`converse` keep their own `timeout` meaning.

`engine.py::_run_step`: `timeout_seconds = step_block.get("timeout_seconds")`;
`deadline = time.monotonic() + timeout_seconds` when set, else `None`.
`invoke(...)` receives `timeout_seconds or 7200`; `_run_model_step` passes
the deadline to the tool loop so a model step stops issuing turns after it;
`serve_capability` receives `deadline`. A broker call that starts with less
than `POLL_FLOOR_SECONDS` left is still sent once (the api may answer
`completed` immediately) and then times out on the first pending answer.
No cap is imposed in the cli; the api fails jobs older than 2 h, so a step
`timeout` beyond 2 h simply observes that failure as `ok: false`.

### D9. Documentation surface (README)

- `apis.<name>: {uses: media}` with the sentence that it takes no `auth`,
  runs only in managed runs, and that `media.process` takes `{attachment,
  outputs, options?}` where `attachment` is a trigger attachment identifier.
- Managed providers: what `managed: true` on a contract means for the
  binding (`auth` optional when the provider accepts kinds, forbidden when
  it accepts none), that grants are static, that local runs need a credential
  or fail with `integration.managed_unavailable`.
- Step `timeout` for agent and model steps, default 30m when the step grants
  a media capability.
- Run record: `attempts` on managed calls, the two new events.

## Phase 2: Implementation order

1. `pyproject.toml` pin `outcomeci-connectors>=0.15.0,<0.16`; regenerate the
   dev environment. Everything below compiles against the new contract.
2. `language.py` D1 (connection shape) + `compiler.py` D1 validation. Run
   `tests/test_tavily.py` and `tests/test_custom_connector.py`; update the
   Tavily message match and add `managed` assertions.
3. `executor.py` D2 dispatch and `_managed_response`. `test_tavily.py`
   `test_platform_key_requires_managed_execution` keeps passing with the new
   message; add the no-`state` compatibility test.
4. `broker/pending.py` D3 with its unit tests (no I/O).
5. `policy.py` D4 (resume, attempts, statuses, events) + `server.py` and
   `engine.py` plumbing (D7, D8 engine side).
6. `client.py` cancel + `main.py` wiring (D7).
7. `language.py` D6 grant/registry/warning rules + `compiler.py`/`cli.py`
   `validation_warnings`; D8 `timeout` lowering and default.
8. README (D9). Full `pytest -q`, `ruff`.

## Phase 3: Test plan

All tests fake the provider through `monkeypatch.setattr(language, "providers",
lambda: {...})` as `tests/test_google_drive.py` does, so they do not depend on
the installed connectors beyond import; `tests/test_media.py` imports
`outcomeci_connectors.providers.media.PROVIDER` where the real contract is
wanted and builds a fake `Provider(managed=True, base_url=None, ...)` with a
tiny registry where a smaller one is clearer.

`tests/test_media.py`:

- **Compile rules (D1, D6; US1.1, US2.1-2.4, SC-003)**
  - `test_media_binding_compiles_to_a_managed_connection_without_credential`:
    `connection == {ref, provider: http, base_url: None, managed: True, auth:
    {connector: media, accepts: [{kind: none, ...}], managed: True},
    allow_private_network: False}`; `connectors["transcribe"]["provider"] ==
    "media"`.
  - `test_media_rejects_auth`: `auth: secrets.x` → `ConfigError` matching
    `apis.transcribe.auth is not used: media takes no credential`.
  - `test_managed_grants_are_static`: `outputs: trigger.wanted` →
    `grants on a managed api must be static values`.
  - `test_unknown_output_type_names_the_type`: `outputs: [subtitles]` →
    message contains `subtitles` and `transcript`.
  - `test_outputs_must_be_a_list_of_strings`: `outputs: transcript` (scalar)
    and `outputs: []` both fail naming `.outputs`.
  - `test_stale_contract_is_a_compile_error`: a fake provider whose contract
    lacks `managed` → message names `outcomeci-connectors>=0.15.0`; one
    whose contract has `media: {}` → same.
  - `test_cron_trigger_warns_about_attachment_source`: compiles; `validation_warnings`
    has one entry naming `apis.transcribe` and `media.process`; an `email`
    trigger produces none.
  - `test_media_step_defaults_to_a_30_minute_timeout` and
    `test_explicit_timeout_is_lowered`: `block["timeout_seconds"]` is 1800 /
    `duration_seconds("5m")`; a Tavily-only step has no `timeout_seconds`.
  - `test_no_tavily_literal_in_sources` (SC-001): grep `src/outcomeci` for
    the literal; the only permitted matches are none (the connectors registry
    adapter lives in the connectors package).
- **Dispatch and local runs (D2; US1.2, US2.5-2.6, SC-004)**
  - `test_managed_call_routes_through_connector_client_with_fingerprint`:
    fake `connector_client` asserts the journal already holds `request_id`,
    receives `input` unchanged (`attachment` forwarded verbatim), and no
    `resolver`/`transport` is touched.
  - `test_local_run_fails_before_any_request`: no `connector_client` →
    `integration.managed_unavailable` naming `media`; the journal has the
    denied call; an `httpx.MockTransport` that fails the test on any request
    proves no network.
  - `test_receipt_required_without_request_id`: `IntegrationExecutor.execute`
    directly, `request_id=None` → `integration.receipt_required`.
  - `test_response_without_state_is_completed` (older api).
  - `test_missing_requested_output_type_is_response_invalid`.
- **Polling (D3, D4; US1.3-1.4, US3; SC-002, SC-005, SC-006)**
  - `test_poll_backoff_floor_and_cap`: pure `pending.poll` with a fake
    clock: `retry_after_seconds` 0/None → 10; 2 → 5; 300 → 60; remaining
    budget clips the last wait.
  - `test_pending_twice_then_completed_is_one_request_id`: scripted client
    answers pending, pending, completed; exactly three `connector-call`
    requests with one `request_id`; the agent gets the completed output;
    `calls.json` has `attempts` of length 3, states `[pending, pending,
    completed]`, two `integration.pending` events then `integration.completed`
    with `minutes`.
  - `test_resume_after_crash_reuses_the_request_id`: write a journal with a
    `pending` managed call (as a crash before the first acknowledgement
    leaves it), build a new `PolicyExecutor` on the same directory, execute
    the same proposal: no `integration.proposed`, the client sees the old
    `proposal_sha256` as `request_id`, zero new call entries.
  - `test_resume_matches_a_pending_call_across_revisions`: same journal with
    a different `revision` in the compiled document; still reused.
  - `test_failed_completion_returns_failure_category_to_the_agent`: `ok:
    false, failure_category: input_invalid, output.error:
    integration.attachment_not_found` → call status `failed`, result returned
    (no raise), `integration.failed` event carries both fields.
  - `test_timeout_cancels_once_and_raises_integration_timeout` (SC-006):
    deadline 60 s, client always pending; exactly one cancel request for the
    `request_id`; call status `cancelled`, `receipt_state == "cancelled"`,
    `attempts[-1]["kind"] == "cancel"`, `integration.cancelled` with
    `reason: timeout`, raised `integration.timeout`.
  - `test_cancel_failure_is_journalled_and_timeout_still_raised`: cancel
    raises `CoreError("core_rejected")`; the attempt has `error`; the
    outcome is unchanged.
  - `test_run_cancelled_predicate_stops_polling`: `run_cancelled` flips to
    true after the first pending; one cancel; raised `integration.cancelled`
    with category `cancelled`; `workflow_failure_category` → `agent_cancelled`.
  - `test_api_cancelled_state_is_recorded_as_cancelled`: client answers
    `state: cancelled`; no cancel request is sent; status `cancelled`.
  - `test_retryable_core_error_mid_poll_is_one_more_attempt`.
  - `test_two_proposals_are_two_request_ids` (US3.5).
  - `test_dataset_export_check_passes_with_attempts` (SC-005): build a run
    directory with the records of the pending-twice run and assert
    `completeness(...)["complete"]` and that `calls` decodes with `attempts`.
- **Runner client (D7)**
  - `test_connector_cancel_posts_lease_and_request_id`: `MockTransport`
    asserts path `.../connector-call/cancel` and body; 404 → `CoreError`
    non-retryable; 409 → `core_conflict`.
  - `test_execution_options_wire_cancel_and_heartbeat_failure` (in
    `tests/test_cloud_runner_flow.py`):
    `run_cancelled()` is false, becomes true once `heartbeat_failure` has an
    entry; `connector_cancel(request_id=...)` reaches the client with the
    lease.

`tests/test_tavily.py` changes: `match="grants on a managed api must be
static"`; assert `connection["managed"] is True` in both BYOK and platform
cases; assert BYOK managed-run calls still go through the proxy
(`test_proxy_never_resolves_credentials...` already does, keep it);
`calls.json` for a Tavily call has no `attempts`/`managed` keys.

`tests/test_custom_connector.py`: assert `connection["managed"] is False`
and that `base_url` is still required (keys on `base_url_from`, not on
`base_url is None`).

## Phase 4: Release

- **Order.** connectors v0.15.0 on PyPI first (it carries `managed` and
  `media`); then this cli release; then the api promotes its pin and deploys;
  then shared-infra pins the runner image digest. Until the api deploys,
  managed runs on the new cli only ever see `state: "completed"` from Tavily
  and a `media` binding fails at the api with 403 (provider not registered),
  which the cli reports as `CoreError("core_rejected")` → `uncertain` call.
  Acceptable for the window; the www side shows the api's reason.
- **Runner image.** `Dockerfile.runner` resolves `outcomeci-connectors` from
  PyPI at build time through the `pyproject.toml` pin; no Dockerfile change,
  but the first runner build after this merge is the one that picks up
  0.15.0, and shared-infra must pin that digest.
- **Release note (user-facing, cli release and README):**
  "`outcomeci-connectors` 0.15.0 adds a `managed` key to every provider
  contract. Every workflow that binds an api therefore gets a new provider
  digest and a new `workflow_revision` on its next compile, once. Nothing in
  the workflow changes; `oci workflow sync` will record the new revision.
  Runs paused before the upgrade resume under their cloud revision id, which
  does not depend on the compiled hash." Also: new `timeout` field on agent
  and model steps; new `integration.pending` / `integration.cancelled`
  events and `attempts` on managed calls in `calls.json`; the
  `integration.managed_unavailable` message now names the provider.
- **Conventional title**: `feat(media): managed provider calls with pending
  connector-call responses and step timeouts`.

## Complexity Tracking

| Decision | Why Needed | Simpler Alternative Rejected Because |
|----------|------------|-------------------------------------|
| Connection-level `managed` in addition to `auth.managed` (D1) | Tavily BYOK in a managed run is still proxied; "provider is managed" and "platform supplies the credential" are two facts that differ for that case | Reusing `auth.managed` for both would either stop proxying BYOK Tavily (behaviour change, FR-011) or make a credential-bearing connection claim a platform credential. |
| Poll loop in `pending.py` called from `policy.py`, not inside the executor (D3, D4) | The journal (attempts, events, statuses) is written under the policy lock; the executor has no journal and must stay "one request per call" so each attempt is recorded | Looping inside `IntegrationExecutor.execute` would hide attempts from the journal or need a journal callback threaded through the executor; a loop in `main.py`'s `connector_client` lambda would be untestable without a runner and could not reach the step deadline. |
| Resume lookup by `(step, capability, request)` for pending managed calls (D4) | The fingerprint includes the compiled revision hash, which moves with the runner's connectors build; a crash mid-poll followed by a resume on a newer runner image must still reuse the api's receipt | Making the fingerprint ignore the revision for managed calls would change `request_id` for every managed call, including Tavily, and break the api's stored receipts during the upgrade window. |

## Deviations other repos must know

1. **Grant `outputs` matching is equality, not subset** (R10). A step whose
   grant pins `outputs: [transcript]` must propose exactly `[transcript]`.
   The api's authorize path applies "today's static-grant algorithm"; if it
   implements subset for `outputs`, the cli is the stricter of the two and
   the agent sees `integration.grant_denied` locally before any request.
   Revisit when a second output type exists.
2. **`ok: false` receipts are returned to the agent, not raised.** The api's
   `output.error` codes (`integration.attachment_not_found`,
   `integration.media_unsupported`, `integration.output_unsupported`,
   `integration.options_unused`, `integration.failed`) and `failure_category`
   reach the agent verbatim as the tool result and the journal records the
   call as `failed`. Only `integration.timeout` and `integration.cancelled`
   are raised and stop the step. The www run-log should render
   `failure_category` from the `integration.failed` event.
3. **Resume across runner builds relies on journal lookup, not on the
   fingerprint.** The api must keep accepting a stored `request_id` whose
   stored input digest matches even though the cli's compiled revision has
   changed; it does (the digest is over `input`, not the revision), so no api
   change, but the api must not add the revision to its digest.
4. **`connector-call/cancel` 404/409 are not errors for the runner.** The
   poller journals them and keeps its own outcome (`timeout` or
   `run_cancelled`); the api should not expect a retry.
5. **Heartbeat volume.** Every poll emits one `integration.pending` event
   through the heartbeat (`policy_event`), up to 180 per 30-minute step at
   the api's 10 s `retry_after_seconds`. If the api wants fewer, it raises
   `retry_after_seconds`; the cli honours up to 60 s.
6. **Step `timeout` has no cli cap.** The api's 2 h job reaper is the
   effective ceiling; a longer step timeout observes that as `ok: false,
   failure_category: timeout` from the api rather than a cli timeout.
7. **No panel work here.** `usage.minutes` is kept in the result and added to
   the `integration.completed` / `integration.failed` event as `minutes`;
   the api usage feed and www panel own the display (FR-010).
8. **Warnings channel.** `oci workflow validate` prints a new
   `validation_warnings` list (strings) next to `capability_warnings`; the
   MCP `validate_workflow` and www validation views may surface it.
