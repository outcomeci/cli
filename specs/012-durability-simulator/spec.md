# Feature Specification: Durability simulator

## Purpose

OutcomeCI needs executable ecosystem-level tests whose unit is a versioned user persona journey. A dedicated container must behave like a new local-first user, exercise public OutcomeCI surfaces, interrupt the journey, and prove recovery reaches one valid state without duplicating committed work or leaking credentials.

## User Scenarios

### Platform release verification

An OutcomeCI maintainer runs the published proof-runner image with an empty persistent directory. It interprets the bundled `local-first-v1.proof.yml`: initialize OutcomeCI, configure a workflow, create a local Vault credential, use it through a workflow capability, run a local outcome, inject documented process failures, resume, and exit zero only when every durability assertion passes.

### Failure diagnosis

When a simulation fails, the maintainer can inspect a JSON report and hash-linked event ledger to see the last durable transition, injected fault, recovery action, and failed invariant without seeing credential values.

### Future customer workflow verification

A later persona may select a mounted `outcome.yml` and persona inputs. V1 must reject unsupported actions and sources so the reference journey certificate is never confused with customer-workflow certification.

## Requirements

- Define one persona per `proof.yml` with `apiVersion`, `kind`, metadata, starting state, ordered journey actions, fault injections, assertions, and output paths.
- Provide `oci proof run` with `--definition`, `--workspace`, and optional `--report` arguments; default to the packaged local-first persona.
- Start from an empty persona workspace and initialize a filesystem-backed workflow through the normal repository setup surface.
- Initialize an encrypted local Vault, store a synthetic credential, wire it to a phase capability, and execute that capability through the credential-blind broker.
- Run each lifecycle action in a fresh process using the installed package.
- Inject at least one hard process exit after phase artifacts are durably written and before validation commits phase completion.
- Recover using the same persisted workspace, validate the artifacts, resolve the required human gate, and progress through intake, plan, and tasks.
- Prove completed phases contain no duplicates, rejected/replayed transitions cannot advance state, the Vault remains decryptable across processes, required artifacts exist, and the final state is `ready_for_implementation`.
- Write an append-only, hash-linked JSON Lines ledger and a stable `outcomeci.proof-report/v1alpha1` JSON report.
- Never serialize secret values, environment credentials, or Vault keys into logs, reports, or the ledger.
- Exit zero only when every assertion passes; otherwise exit non-zero while retaining evidence.
- Provide a dedicated non-root proof-runner image and CI job that builds, scans, and executes the reference persona.

## Non-goals

- Calling a real model provider.
- Measuring model output quality.
- Destructive chaos against production infrastructure.
- Certifying arbitrary user workflows in v1.

## Acceptance Criteria

- The local-first-v1 persona passes inside the proof-runner image with external networking disabled.
- Re-running against a clean workspace yields a new valid certificate.
- A deliberately invalid definition, action, workflow source, or assertion fails closed.
- Unit and integration tests cover report integrity, fault recovery, idempotency, secret redaction, and CLI behavior.
