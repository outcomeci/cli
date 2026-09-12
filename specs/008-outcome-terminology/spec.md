# Feature: Canonical workflow-run CLI contract

Make `oci` consume and emit `outcome_run_id` and use Standup terminology for its engineering-context artifact. No compatibility with the unpublished legacy field is required.

## Acceptance criteria

- Claims and manifests use `outcome_run_id`.
- Generated engineering-context artifacts use Standup naming.
- CLI tests and documentation describe the canonical contract.
