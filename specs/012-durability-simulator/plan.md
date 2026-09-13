# Implementation Plan: Durability simulator

## Architecture

Add a small `outcomeci.simulation` persona interpreter and `outcomeci.simulation_step` child-process entrypoint. The interpreter creates an empty persona workspace, records hash-linked journey events, validates the allowlisted declarative actions, and invokes every setup or state transition through a new Python process. The child initializes the repository and Vault, configures and exercises a credential-blind capability, materializes deterministic contract-compliant outcome artifacts, optionally exits with a reserved injected-fault code, validates through the existing local lifecycle, and returns JSON only after durable writes.

The simulator uses repository initialization, local Vault, `IntegrationExecutor`, `local.begin`, `local.compile_context`, `local.validate_artifacts`, `local.respond`, and `local.advance`; it does not implement parallel versions of those systems. Assertions and fault metadata are written to a stable persona report contract.

## Container

`Dockerfile.proof-runner` builds the same wheel as `Dockerfile.runner`, installs only runtime dependencies, runs as UID/GID 10001, and enters the packaged local-first proof. The image requires a writable `/proof` volume and no external network. A dedicated workflow builds, scans, and executes it; internal publication follows the outcome runner's ECR pattern after the journey succeeds.

## Safety

- Generate a synthetic canary secret and assert it never appears in the report or ledger.
- Store the Vault key in a separate proof-runner config directory with mode 0600.
- Use atomic report writes and fsync each ledger entry before the next process transition.
- Scope cleanup to a newly generated run directory beneath the caller-selected workspace.
- Reserve explicit schema, persona, and journey versions for future compatibility.

## Verification

- Unit tests for hash-chain verification and redaction.
- Subprocess integration test for crash-after-write recovery and exact-once phase completion.
- CLI test for success and unsupported-definition failure.
- Local Docker build and `--network none` run of the bundled local-first-v1 persona.
