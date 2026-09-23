# Major version release review

DRY/hygiene review of the whole `src/outcomeci` tree ahead of the next major
version, done via five parallel reviews (core engine, CLI surface, cloud +
cloud_runner, Slack/vault, proof_runner) plus `ruff check`/`ruff format
--check` (both already pass 100% clean — no unused imports, no simplify
violations, no TODO/FIXME/XXX anywhere in `src`).

Checkboxes are for tracking work through this list, not a judgment already
made — check one off once it's actually fixed and verified, not just filed.

## Release blockers

- [x] **Production docs proof points at a domain with no app behind it.**
  `src/outcomeci/proof_runner/docs.py:25` —
  `DEFAULT_DOCS_BASE_URL = "https://sparepartslabs.com/api/docs/raw/outcomeci/next"`.
  The bare `sparepartslabs.com` apex only routes to `assets.sparepartslabs.com`
  (the CDN bucket) in shared-infra — no ALB, no CloudFront, nothing serves
  that host for this path. The real provisioned docs/marketing domain is
  `outcomeci.com` / `www.outcomeci.com`. Staging gets this right
  (`staging.outcomeci.com`); production doesn't. The same wrong value is also
  in the still-open shared-infra PR (`docs-proof.tf:25` on
  `origin/docs-proof/hourly-ecs-tasks`) — fix both before either merges, or
  `docs-quickstart-v1`'s hourly production run fails to connect every hour,
  forever.

- [x] **The claim-conflict retry fix (#70) only covers 1 of 4 identical call
  sites.** `src/outcomeci/cloud_runner/main.py` — `execute_workflow()`'s
  `claim_workflow()` call is wrapped to treat a retryable `CoreError` as a
  clean no-op. `authorize()`'s `claim_authorization()` (line 499),
  `execute()`'s `claim_execution()` (line 593), and `execute_publication()`'s
  `claim_publication()` (line 766) go through the identical mechanism in
  `client.py` and are still unwrapped — the exact failure mode #70 was
  written to eliminate can still happen on three of the four entry points.

- [x] **Webhook oversized-payload error always claims a 1 MiB limit, even
  when the real limit is 2 MiB.** `src/outcomeci/local.py:907`. Known and
  deliberately deferred when webhook-trigger-v1 was built; interpolate the
  actual `limit` into the message.

- [x] **The proof framework's own leak-detection has a blind spot for JWT
  credentials.** `credentials.never_exposed` scans for the literal marker
  `oci_vault_proof_`, but the JWT credential's secret is a real RSA private
  key generated in `src/outcomeci/proof_runner/credentials.py:104-120` that
  never contains that marker. It's the one credential type whose value is
  code-generated rather than operator-supplied, and it's silently exempt
  from the assertion whose whole job is catching leaked secrets.

## Cross-cutting: one atomic-write helper, not nine

- [x] `tmp = path.with_suffix(".tmp"); tmp.write_text(...); tmp.replace(path)`
  is hand-rolled 9+ times: `local.py:573`, `worker.py:16`, `slack.py:427`,
  `humans.py:60,134,215`, `local_vault.py:70`, `proof_runner/simulation.py:194`,
  `proof_runner/step.py:52`. This is the durable-state write path for run
  state, vault, worker records, interactions, and proof ledgers. One shared
  `atomic_write_json(path, value)` collapses all of it.

## CLI surface (cli.py, process.py, policy.py, capability.py, custom.py, tunnels.py)

- [x] `process.py`'s `invoke()` and `invoke_conversation()` duplicate ~50
  lines of per-agent (codex/claude/opencode) credential/env logic — root
  cause of both showing up as complexity outliers (39 and 16).
- [ ] `cli.py`'s `--workspace` flag means three incompatible things across
  subcommands (local path / cloud workspace ID / fixed container mount)
  with no type-level distinction. Consider renaming the cloud-ID ones to
  `--workspace-id` now, since it's a breaking change either way and this is
  the release to make it in.
- [x] `cli.py` has a working `_print_json`/`_workflow_path` helper that
  ~40% of call sites bypass by hand (16 sites for the former, 4 for the
  latter) — mechanical fix.
- [x] `policy.py`'s `execute()` repeats a "deny → event → save → raise"
  triplet 4 times.
- [x] `capability.py`'s two invoke functions duplicate the entire broker
  socket exchange.
- [x] `custom.py`'s two MCP transports hardcode the same `initialize`
  payload separately — risk of protocol-version drift between them.
- [x] Inconsistent eager vs. lazy imports in `cli.py` (`tunnels`,
  `webhooks.listen`, `debug.run`, `slack_vault.sync_credentials` lazy,
  everything else eager) with no documented reason.

## Cloud runner (cloud.py, cloud_runner/)

- [x] Six near-identical "unwrap `detail`, raise `ExecutionError`" blocks in
  `cloud.py` — one helper.
- [x] Codex credential-file writing is implemented three times with two
  different safety postures (one uses atomic `O_EXCL`, two use plain
  `write_text`+`chmod`) and two different directory names (`.codex` vs
  `codex`) for the same artifact.
- [x] The SIGTERM→wait→SIGKILL sequence and the temp-workdir-with-chmod-
  and-cleanup scaffold are each duplicated across `main.py`'s four entry
  points.
- [x] Minor: `redaction.py`'s dict/list branches are dead code in
  production (only ever called with strings); a second, differently-shaped
  redaction implementation lives in `integrations.py:306` and a third regex
  bank lives in `execution_events.py` — worth deciding if these three
  should converge.

## Core engine (local.py, config.py, repository.py)

- [ ] `local.py`: `_execute`/`continue_run`/`retry`/`respond`/`trigger` all
  redeclare and forward the same 7-keyword signature — a dataclass would
  remove ~40 lines and prevent a new option being forgotten in one caller.
- [ ] `local.py`: `start()` and `trigger()` duplicate the run-bootstrap
  dict + before-gate logic.
- [ ] `local.py:691`: the single largest, most behaviorally significant
  agent prompt is the only one of five not extracted to `templates.py`,
  breaking the file's own convention.
- [x] `local.py`: `_validate_required_effects` and `_write_effect_receipts`
  independently re-derive the same "did this broker call actually succeed"
  check — two copies to keep in sync.
- [x] `config.py`: a 14-times-repeated non-empty-string guard with
  inconsistent error wording has no shared helper, unlike the file's
  existing `_mapping()` pattern.
- [ ] `config.py`: `_load_v1alpha1` (complexity 81) inlines two ~80-line
  blocks instead of following its own file's helper-extraction convention
  used everywhere else in it.
- [x] `repository.py`: the two agent-skill mirror paths (`.agents/skills/...`,
  `.claude/skills/...`) are hardcoded independently in two functions.
- [x] `config.py:1208`: a dangling orphan comment left over from a prior
  edit (confirmed via `git blame`) — delete or rewrite.

## Slack/vault (slack.py, slack_vault.py, humans.py, local_vault.py, publication.py)

- [x] `targets()` and `_directory()` in `slack.py` both re-fetch the same
  Slack API data and duplicate the same filter logic — doubles API calls
  per invocation.
- [x] Path-validation line duplicated verbatim between `local_vault.py` and
  `slack_vault.py`.
- [x] Architecture question resolved: `slack_vault.py` is not redundant
  with `local_vault.py` — it correctly composes (`local_vault.initialize`/
  `put`, `cloud.vault_request`) rather than duplicating; the only overlap
  is the path-validation line above.
- [x] Security check: no token/secret is ever logged, printed, or embedded
  in an exception in this cluster. Clean.

## proof_runner (built this session)

- [ ] `step.py`'s `execute()` (complexity 42) has 12+ branches sharing a
  byte-identical 3-line dispatch body — a `{action: handler}` table would
  roughly halve its complexity.
- [ ] The "evaluate checks → raise → write final_state → return" tail is
  duplicated across all 6 proofs' assertion functions in `step.py`.
- [ ] Three independent hand-rolled mock-HTTP-server implementations
  (`step.py`, `credentials.py`, `cloud_vault.py`), two with a byte-identical
  `_send()` helper.
- [x] `cli.py`'s `--name` choices tuple is manually kept in sync with the
  `proofs/*.proof.yml` directory rather than derived from it (minor).
- [x] Checked and clean: no leftover duplication from the PR #64/#65/#67
  consolidation in `tests/test_proof_runner.py`; all 6 proof YAMLs are
  structurally consistent.

## Complexity hotspots: real vs. justified

Real, tied to findings above: `cli.py:main` (90 — dispatch-chain, bigger
decision, not a quick fix), `config.py:_load_v1alpha1` (81),
`process.py:invoke`/`invoke_conversation` (39/16), `proof_runner/step.py:execute`
(42), `cloud_runner/main.py:execute_workflow` (40, mostly the temp-workdir
scaffold).

Checked and justified as inherent domain branchiness, not sprawl:
`webhooks.py:execute`, `slack_vault.py:sync_credentials`, `slack.py:deliver`,
`config.py`'s other schema validators (`_human_interactions`,
`_integrations`, `_triggers`), `cloud_runner/main.py`'s other three linear
pipelines.

## Lower priority, worth a decision but not urgent

- [ ] `twin.py`/`cloud.py` hand-roll `urllib.request` while the rest of the
  repo (including everything built this session) uses `httpx`, already a
  dependency — pick one.
- [ ] No cli-specific `.sp/memory/constitution.md`, unlike `api`/
  `shared-infra`/`homebrew-tap`. The workspace huddle log says one was
  ratified for this repo but it isn't in the checkout.
- [ ] Stray local git worktrees under `.worktrees/` and
  `/tmp/outcomeci-cli-*` — not code, housekeeping only.
