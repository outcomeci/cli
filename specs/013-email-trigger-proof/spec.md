# Feature: Required workflow triggers and email proof

Every Outcome workflow declares at least one trigger. V1 supports `manual` and
`email.received`; manual preserves explicit local execution while email carries
safe MIME metadata and encrypted artifact references into a managed run. Add a
cloud-connected proof persona that synchronizes an email workflow, requests a
real test email, waits with a bounded timeout, and certifies exactly-once
dispatch, encrypted artifacts, metered usage, and a structured receipt event.

## Acceptance criteria

- Compilation rejects missing, duplicate, malformed, or unsupported triggers.
- Phase inputs may reference a declared trigger as `trigger.<name>`.
- The default template explicitly declares a manual trigger.
- `email-trigger-v1.proof.yml` never receives AWS credentials or email content.
- Proof output is redacted, correlated by `proof_id`, and contains real usage.

