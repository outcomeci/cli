# Implementation Plan: Credential-blind integration executor

## Technical Context

- Python 3.11+ package with the existing PyYAML and jsonschema compiler stack.
- Add HTTPX for a maintained synchronous HTTP transport with bounded redirects/timeouts.
- Preserve the current `outcome.yml` list-form connection contract while accepting mapping form for concise authoring.
- Implement the executor as importable library code; CLI commands are adapters, not subprocess architecture.

## Constitution Check

- Credentials never enter compiled artifacts, agent prompts, or result bodies.
- Workflow configuration remains deterministic, reviewable, and versioned.
- Network authority is phase-scoped and least-privileged by default.

## Design

1. Normalize HTTP connections, integrations, operations, and phase capabilities during compilation.
2. Build a registry from compiled workflow capability names to immutable execution policies.
3. Resolve credential references inside the executor and inject auth immediately before transport.
4. Project output before returning it and emit metadata-only audit records.
5. Generate standalone patch documents and apply them only against the expected compiled parent revision.

## Project Structure

- `src/outcomeci/config.py`: schema normalization and compiled capability manifest
- `src/outcomeci/integrations.py`: executor, auth, safety, templating, projection, patches
- `src/outcomeci/cli.py`: inspect/execute/patch commands
- `tests/test_integrations.py`: contract, boundary, and lineage tests
- `README.md`: authoring and command guide
