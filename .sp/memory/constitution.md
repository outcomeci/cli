<!--
Sync Impact Report
==================
Version change: none -> 1.0.0
Rationale: First ratification. The workspace huddle log
(../.sp/huddles/002-repository-constitutions/huddle.md) lists a "spareparts-cli" row as
complete, but that predates the monorepo split and named a different, now-defunct repo --
this file was never actually committed to outcomeci/cli. There is no prior version to
amend from.

Principles: five evidence-grounded Review Pillars, written directly from a DRY/hygiene
review pass done in this repo (outcomeci/cli PR #71, MAJOR_VERSION_REVIEW.md) rather than
imported from a template:
  I. Extract on the Second Copy of Anything Security- or Correctness-Relevant
  II. A Shared Helper Preserves Behavior, Not Just Structure
  III. A Client or Primitive Swap Is Not Done Until It Is Tested Directly
  IV. Ruff Clean Is the Floor; a Complexity Flag Is a Question, Not a Verdict
  V. Proofs Are Evidence, Not Fixtures

Added sections: Relationship to the Workspace Constitution, Repository Context, Review
Scope, Review Pillars (I-V), Review Output Format, Governance.

Templates: this repo has no .sp/templates/ or .claude/commands/constitution.md yet, so
none were checked. A future amendment that adds Spec Kit scaffolding here should confirm
this file's Constitution Check gate resolves correctly against it.

Deferred / omitted: no pillar on type-checking (this repo runs no mypy or equivalent
today; see Repository Context) or on CLI UX conventions (no evidence base yet -- would
need to be written from real precedent, not invented).
-->

# OutcomeCI CLI Constitution

This is the practical review standard for this repository: the `oci` command-line
runtime, its local and cloud execution engines, and the proof-runner regression harness
that tests them against real behavior. It is a reviewer's working document, not a style
guide to read once.

## Relationship to the Workspace Constitution

The workspace constitution (`../.sp/memory/constitution.md` at the workspace root, if
present in this checkout) holds the patterns shared across every Spare Parts Labs repo:
commit hygiene, user-facing copy rules, docs tense, and the review findings format.
Reference it for those shared patterns. Where it and this constitution disagree, **this
constitution takes precedence** for work in this repo.

## Repository Context

A reviewer needs these facts before reading a diff here.

- Lint is `ruff` with `select = ["E4", "E7", "E9", "F", "I", "UP", "B", "SIM"]`
  (`pyproject.toml`) -- pyflakes, isort, pyupgrade, bugbear, simplify. There is **no
  mypy or other type checker** configured anywhere in this repo. A `# type: ignore`
  comment does nothing here and is dead weight; delete it rather than write a new one.
- Releases are generated from Conventional Commit history via `commitizen`
  (`[tool.commitizen]` in `pyproject.toml`, `semver2`, `tag_format = "v${version}"`).
  Every commit MUST use Conventional Commits, and a breaking change MUST use `!` or a
  `BREAKING CHANGE:` footer -- `publish.yml`'s release notes and the version bump both
  read commit history directly.
- The runtime is layered: `local.py` (filesystem-backed local execution) is called both
  directly and through `cloud_runner/main.py` (the ECS-hosted cloud execution path) and
  `proof_runner/step.py` (the regression harness). A change to a shared entry point's
  behavior or signature has at minimum these three callers, plus `cli.py`, `worker.py`,
  `webhooks.py`, and `debug.py` -- grep for every caller before changing a signature in
  `local.py`, not just the one you were looking at.
- `src/outcomeci/proof_runner/` is a second, parallel product: OutcomeCI's own
  regression harness for OutcomeCI, built to prove the real CLI (and for
  `agent-driven-v1`, a real agent; for `email-trigger-v1`, real cloud infrastructure)
  behaves the way the docs and contracts claim. It ships in the same wheel as the
  product it tests but is demarcated in its own subpackage on purpose. See
  `src/outcomeci/proof_runner/__init__.py`'s module docstring.

## Review Scope

Review the diff, not the whole repository. A pre-existing issue outside the changed
lines is out of scope unless the diff makes it materially worse. Findings must cite
`file:line` from code actually read in this repo, not a guess at what a file "probably"
contains.

## Review Pillars

### I. Extract on the Second Copy of Anything Security- or Correctness-Relevant

Two copies of ordinary logic can wait for a third before extraction (see Pillar IV's
note on complexity: branchiness is not automatically duplication). Two copies of a
security check, a credential-handling step, or a "did this operation actually succeed"
check MUST be extracted the moment the second copy appears, because independently
maintained copies drift, and the drift is silent until something depends on the
difference.

- **Precedent for the danger**: `local.py`'s `_validate_required_effects` (the
  must-confirm gate that blocks a phase from completing) and `_write_effect_receipts`
  (the reported artifact) each independently re-derived "did this broker call actually
  succeed" from the same journal entry shape -- and had already drifted: one used a
  strict `result.get("ok") is True` check, the other a looser `bool(result.get("ok"))`.
  Neither was wrong on its own; the two were silently answering a safety-relevant
  question differently. `_call_succeeded()` (`local.py:178`) is the fix and the
  precedent: one function, both callers, one answer.
- **Precedent for the pattern**: `security.py`'s `atomic_write_text`/`atomic_write_json`
  (`security.py:19,35`) replaced the tmp-write-then-replace idiom hand-rolled 9 times
  across `local.py`, `worker.py`, `slack.py`, `humans.py`, `local_vault.py`, and both
  `proof_runner/simulation.py` and `proof_runner/step.py` -- the durable-write path for
  run state, vault, worker records, interactions, and proof ledgers. Two of those nine
  copies had already diverged in a way that mattered (one needed 0600 permissions from
  creation, one preserved a suffix the others didn't); this is exactly the kind of place
  a real divergence goes unnoticed until it's load-bearing.
- **Flag**: a new copy of an existing auth check, size/read-bound, secret-marker
  convention (see Pillar V), or success/failure classification, where an existing helper
  already answers the same question elsewhere in the file or a sibling module.
- **Commend**: a PR that finds and reuses an existing check instead of writing a new one
  that happens to agree with it today.

### II. A Shared Helper Preserves Behavior, Not Just Structure

Extracting a helper is not a license to also normalize the things that differ between
call sites. Decide deliberately which differences are accidental (safe to unify) and
which are intentional (must stay per-caller), and say which is which in the commit.

- **Precedent, differences that must stay per-caller**: `config.py`'s `_non_empty_str()`
  (`config.py:41`) collapsed a `isinstance(x, str) and x.strip()` guard repeated 14
  times, but every caller kept its own message text unchanged -- a workflow author
  reading `"timezone is required"` needs that to stay distinct from
  `".subject_prefix must be non-empty"`. Only the identical mechanical check moved.
- **Precedent, a difference worth normalizing on purpose, stated as such**: `cloud.py`'s
  `_raise_for_status()` (`cloud.py:22`) unified six copies of "unwrap `detail`, raise
  `ExecutionError`" and, while already rewriting the function, also normalized a
  pre-existing asymmetry (a malformed JSON body crashed uncaught on the success path but
  was caught on the error path) rather than carrying two behaviors that had no test
  coverage distinguishing them either way. The commit said so explicitly.
- **Precedent for the same discipline in security-sensitive code**: the httpx migration
  (`cloud.py`, `twin.py`, `tunnels.py`, `custom.py`, `outcome.py`,
  `cloud_runner/client.py`) preserved every bounded-read cap as a true streaming cap
  (`cloud_runner/client.py`'s `_read_bounded()`, `cloud_runner/client.py:20`) rather than
  a buffered full-body read checked for size after the fact, and preserved
  redirect-following per the trust level of the endpoint -- `follow_redirects=True` for
  fixed, operator-controlled endpoints, `follow_redirects=False` for `custom.py`'s
  workflow-author-configured ones, matching `integrations.py`/`slack_vault.py`'s existing
  posture for that identical threat model. That split is deliberate; collapsing it to one
  setting for "consistency" would be a real regression, not a cleanup.
- **Flag**: an extraction whose diff silently changes an error message, a redirect
  policy, a size cap, or a retry/timeout value with no comment explaining that the
  change is intentional.
- **Flag**: an extraction that forces two genuinely different call shapes into one
  signature for the sake of a shorter diff. `_execute()`'s five callers
  (`local.py`'s `ExecutionOptions`, `local.py:42`) share a keyword bundle because they
  are the *same* execution options; `proof_runner/step.py`'s `_WRITE_AND_RETURN`
  dispatch table only covers the ~10 of ~30 `execute()` branches that share a truly
  identical `(root, context, request) -> write -> return` shape, and deliberately leaves
  the other ~20 as explicit branches rather than wrapping every action in one interface.

### III. A Client or Primitive Swap Is Not Done Until It Is Tested Directly

Swapping a library or rewriting a primitive is not complete when the existing test suite
still passes if that suite only exercised the function through a mock at a higher layer.
A test that mocks the function you just rewrote proves nothing about the rewrite.

- **Precedent**: `twin.py` had zero test coverage before this repo's httpx migration --
  `tests/test_twin.py` now exercises the real network layer through `httpx.MockTransport`
  (env validation, the bearer token, an HTTP error status, malformed JSON, a non-dict
  result). `cloud_runner/client.py`'s 409-conflict classification had tests, but they
  only ever hit the branch tested; the migration added direct coverage for the success
  path, the oversized-response rejection, and the 401 path
  (`tests/test_cloud_runner_contract.py`) using a `transport:` parameter threaded
  through `CoreClient.__init__` for exactly this purpose. `outcome.py`'s
  `_open_pull_request` was mocked away entirely in the one integration test that touched
  it; it now has its own direct tests too.
- **Flag**: a PR that swaps an HTTP client, a serialization format, or a crypto/encoding
  primitive and whose diff to `tests/` is zero, when the touched function has any
  external caller that mocks it directly (grep for `monkeypatch.setattr(module, "the_function"...)`
  against the function you're changing -- if every test mocks it out, none of them
  exercise your rewrite).
- **Commend**: threading a `transport: httpx.BaseTransport | None = None` parameter (the
  established pattern -- see `integrations.py`, `slack_vault.py`, `twin.py`,
  `cloud_runner/client.py`) through a function specifically to make its real network
  logic testable, rather than leaving it untestable because it "already has coverage
  above it."

### IV. Ruff Clean Is the Floor; a Complexity Flag Is a Question, Not a Verdict

`ruff check`/`ruff format --check` passing is necessary and says nothing about
duplication, dead code, or whether a function's branchiness is inherent to the domain or
unmanaged sprawl. Treat an unfiltered complexity scan (this repo's own `select` list does
not include `C901`) the same way: a lead to investigate, not an automatic finding.

- **Precedent, branchiness that was genuinely sprawl**: `config.py`'s `_load_v1alpha1`
  (complexity 81 before this review) inlined two ~80-line, fully self-contained
  validation blocks instead of following its own file's convention of small
  `_thing(value, field)` helpers used everywhere else in it. Extracting
  `_validate_custom_connection()` and `_validate_phase_graph()` (pure copy-paste, same
  variables, same messages, character for character) brought it to 47 with zero
  behavior change -- verified with a direct script comparing old and new output
  byte-for-byte before trusting the test suite, since this function has almost no
  direct unit tests of its own error strings.
- **Precedent, branchiness that was justified and correctly left alone**:
  `webhooks.py`'s `execute()` is a durable-execution state machine (heartbeat thread,
  retry, uncertain-state receipts); `cloud_runner/main.py`'s `execute_workflow`,
  `execute`, and `execute_publication` are each a genuinely linear single-purpose
  pipeline. Each was checked and left as-is rather than forced into a shared shape for
  the sake of a lower number.
- **Flag**: a review comment that cites a high complexity number as a finding on its own,
  with no read of whether the branches are duplicated logic or genuinely independent
  cases.
- **Flag**: a large function inlining a block that would obviously fit this file's own
  established `_thing(value, field)` or `_thing(root, context, request)` convention (see
  `config.py`, `proof_runner/step.py`'s `_WRITE_AND_RETURN` entries) -- that inconsistency
  with the file's own pattern is the actual signal, not the raw number.

### V. Proofs Are Evidence, Not Fixtures

`proof_runner`'s entire reason to exist is testing the real CLI (and for
`agent-driven-v1`, a real agent; for `email-trigger-v1`, real cloud infrastructure)
against what a real user in each environment actually gets. A proof that mocks out the
thing it claims to prove is worse than no proof, because it looks like evidence.

- **Precedent**: `docs-quickstart-v1` fetches the live-published docs over HTTP rather
  than a copy checked into this repo, specifically so the proof fails the moment the CLI
  and the docs disagree, not months later. `webhook-trigger-v1` exercises the real
  `local.trigger()` contract with no mocking, made possible by a `before` interaction
  that returns before an agent would ever be spawned -- not by faking `local.trigger()`.
- **Flag**: a new proof, or an edit to an existing one, that replaces a real CLI
  subprocess call, a real credential-resolution path, or a real assertion against
  produced artifacts with a hand-copied fixture "to make the test faster" or "more
  reliable." Flakiness in a proof is signal about the real system; silencing it by
  mocking is not a fix.
- **Flag**: a synthetic secret anywhere in `proof_runner/` that does not carry the
  established marker-prefix convention (`oci_vault_proof_`, `oci_sim_` --
  `proof_runner/credentials.py:123`, `proof_runner/step.py:237,504`), so a
  `credentials.never_exposed`-style assertion can grep the ledger/report for it without
  ever retaining a raw secret to compare against. A code-generated secret that cannot
  carry a string marker (e.g. a JWT private key) needs an equivalent check some other
  way, not a silent exemption -- see `proof_runner/credentials.py`'s
  `generate_jwt_credential`, which records one unbroken base64 line of the key body for
  exactly this reason.
- **Commend**: a new proof that reuses `proof_runner/mock_http.py`'s `send_json()` for
  its mock server's response-writing boilerplate, and `_evaluate()`
  (`proof_runner/step.py:189`) for its checks/raise/persist tail, rather than
  hand-rolling either again.

## Review Output Format

Report findings in this order, each citing `file:line | issue | recommended fix`:

- ✅ **Passed**: pillars the diff satisfies, briefly.
- 🔴 **Critical (must fix)**: a new duplicate of a security- or correctness-relevant
  check (Pillar I), a behavior change hidden inside a "pure" refactor with no comment
  (Pillar II), a proof that mocks out the real thing it claims to prove (Pillar V), a
  secret without its marker convention.
- 🟡 **Warnings (may merge with justification)**: a client/primitive swap with no direct
  test of the touched function (Pillar III), inconsistency with a file's own established
  helper convention (Pillar IV).
- 💡 **Suggestions**: optional improvements, including a duplicate that's only appeared
  twice and can reasonably wait for a third occurrence.

Propose targeted diffs, never whole-file rewrites. Skip pure style nits ruff would
already catch; `ruff format` is the arbiter of formatting, not a reviewer's opinion.

## Governance

This constitution governs review and implementation work in this repo. The workspace
constitution governs the shared patterns named above; this constitution wins on conflict
within this repo.

Amendments require a PR that states the pillar added, changed, or removed, and cites
this repo's own evidence for it -- a pillar with no evidence in this repo does not belong
here. Versioning is semantic: MAJOR removes or redefines a pillar, MINOR adds a pillar or
materially expands guidance, PATCH clarifies wording.

All commits MUST use Conventional Commits with accurate types and scopes; `commitizen`
and the release workflow read them directly. Breaking changes use `!` or a
`BREAKING CHANGE:` footer. No AI or tool attribution is allowed in commit messages or PR
descriptions.

**Version**: 1.0.0 | **Ratified**: 2026-09-23 | **Last Amended**: 2026-09-23
