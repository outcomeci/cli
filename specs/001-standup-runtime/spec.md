# Feature Specification: Standup Runtime

## Goal

Publish a clean OutcomeCI CLI that lets engineers initialize and inspect the
Standup framework while allowing OutcomeCI Cloud to delegate bounded outcome
phases to Codex or Claude Code through the same workflow contract.

## Requirements

- Expose `oci init`, `oci update`, `oci validate`, `oci status`, `oci outcome`,
  and `oci twin` without legacy Spare Parts command aliases.
- Store repository context under `.outcomeci/` and call the framework Standup.
- Make `outcome.yml` the orchestration policy and Markdown the readable
  instruction body included in its deterministic compiled revision.
- Run intake, plan, and tasks against immutable product checkouts while writing
  versioned artifacts and evidence only to the state repository.
- Accept only bounded, revision-pinned tools and credentials supplied by the
  OutcomeCI API claim.
- Capture native agent transcripts and normalized token usage.

## Acceptance Criteria

- A fresh repository can be initialized and validated.
- Editing referenced YAML or Markdown changes the compiled workflow revision.
- A local wheel exposes the `oci` executable.
- The Outcome runner container can install that wheel and successfully invoke
  `oci outcome run` from an API claim.
- No public interface writes `.sp/`, invokes `sp`, or generates Huddle naming.

