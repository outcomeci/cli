# OutcomeCI ecosystem consumption roadmap

This roadmap tracks the portable contracts that let people, coding agents, CI
systems, and managed runners discover and execute an OutcomeCI workflow without
receiving credentials or depending on OutcomeCI Cloud.

## Foundation

- [x] Publish a versioned JSON Schema for `outcome.yml`.
- [x] Expose phase-scoped capability discovery through the CLI.
- [x] Represent API calls and human participation through one typed phase
  `integrations` contract.
- [x] Declare side effects, approval requirements, and idempotency for every API
  operation.
- [x] Add a credential-blind dry-run that reports the calls and human requests a
  phase could make.
- [x] Add `oci integration doctor` for configuration, credential-reference, and
  connectivity diagnostics.

## Portability

- [x] Support reusable, versioned integration packages.
- [x] Add `outcome.lock` for reproducible workflow, schema, package, and runner
  resolution.
- [x] Project authorized CLI capabilities as MCP tools without exposing secrets.
- [x] Standardize retryable, permanent, authorization, policy, and validation
  errors across integrations.

## Confidence

- [x] Publish a runnable packaged-integration example; expand the catalog as
  providers are added.
- [x] Publish a baseline conformance suite for third-party runners and integrations.
- [x] Document compatibility and migration guarantees for the current schema version.

## Phase integration contract

Human participation is an integration, not a separate workflow language:

```yaml
agents:
  phases:
    intake:
      integrations:
        - type: api
          capability: linear.create_issue
        - type: human
          timing: after
          id: confirm_intent
          participant: requester
          purpose: Confirm the intent and affected scope.
          interaction: approval
          required: true
```

The compiler may retain compatibility with legacy `capabilities` and `humans`
blocks during migration, but new templates and documentation emit only the
typed integration form.

## Delivery sequence

1. Schema, typed phase integrations, and operation safety metadata.
2. Dry-run, doctor, and a stable error taxonomy.
3. Locking and reusable integration packages.
4. MCP projection, examples, and the conformance suite.
