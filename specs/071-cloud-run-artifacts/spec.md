# Feature: Preserve generic cloud workflow artifacts

Generic OutcomeCI cloud workflow runs must preserve the durable files produced beneath their specific `.outcomeci/outcomes/{run_id}/` directory before the ephemeral runner workspace is removed. The completion payload must include paths, content, and checksums while excluding broker-private and credential-bearing paths.

## Acceptance criteria

- Successful generic cloud workflow completion includes the run's durable artifact bundle.
- Native Codex, Claude Code, or OpenCode traces copied into the run directory are included.
- Paths cannot escape the reported run directory.
- The bundle fails closed above 200 files, 2 MiB per file, or 20 MiB total.
- Vault data, environment files, broker state, and agent credentials are never eligible.
