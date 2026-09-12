# Unified Outcome runner

OutcomeCI CLI owns the open-source cloud runner used for authorization and all workflow phases. It consumes one API-issued claim for `intake`, `plan`, `tasks`, or `implementation`, runs the configured Codex or Claude Code adapter, preserves transcripts and usage, and reports a bounded result. Implementation may mutate target checkouts, push branches, and open pull requests; it must not merge them. The repository publishes the only Outcome runner image, and that image has no dependency on `spareparts-cli` or `spareparts-github-projects`.

Acceptance criteria:
- one `python -m outcomeci.cloud_runner` entrypoint supports authorization and Outcome execution;
- `oci outcome run` supports implementation with explicit writable targets and publication output;
- claims/results reject secrets and unbounded process output;
- the container includes `outcomeci-cli`, Codex, Claude Code, Git, and GitHub CLI at pinned versions;
- tests cover claim parsing, implementation publication, terminal reconciliation, and redaction.
