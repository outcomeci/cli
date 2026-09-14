# Specification: Cloud Runner Lifecycle Logs

Emit a small, sanitized lifecycle stream for cloud outcome jobs without forwarding raw coding-agent output.

## Acceptance criteria

- The runner emits deterministic monotonically sequenced lifecycle events.
- Events identify phase and safe status only; they contain no credentials, prompts, stdout, stderr, or transcript content.
- Log delivery uses the existing leased job credential and cannot fail the underlying run when the API is temporarily unavailable.
- Tests cover payloads and failure isolation.
