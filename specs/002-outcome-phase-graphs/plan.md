# Implementation Plan: Outcome Phase Graphs

**Date**: 2026-09-11 | **Spec**: `spec.md`

## Summary

Replace the fixed Standup phase sequence with a validated DAG whose named
artifact contracts and resolved agent policies compile deterministically. Keep
the current code workflow as the generated default and preserve both local and
managed execution paths.

## Technical Context

- Python 3.11+ package using PyYAML and the standard library.
- Existing configuration compiler in `src/outcomeci/config.py`.
- Filesystem execution in `src/outcomeci/local.py`; managed claims in
  `src/outcomeci/outcome.py`.
- JSON Schema validation added as a small runtime dependency.
- Existing pytest suite supplies compiler, CLI, local-runner, and managed-runner
  regression coverage.

## Design

1. Parse one orchestrator object, user-defined phases, dependency edges, named
   contracts, and optional schema references into normalized internal values.
2. Validate identifiers, path containment, sources, dependency reachability,
   media types, schemas, and acyclicity before producing compiled output.
3. Compile deterministic topological levels and resolved runner/model policy;
   hash normalized YAML plus referenced instruction/schema/context content.
4. Store per-phase state in local runs, deriving ready and blocked sets from the
   compiled graph instead of advancing a fixed current-phase index.
5. Let managed claims select only a phase in the compiler-derived ready set.
6. Construct prompts from the orchestrator, phase instruction, declared input
   payloads, and bounded context; validate declared outputs before success.
7. Update the generated template to express the existing code workflow through
   the generalized schema.
8. Compile phase-local human participation and persist local interaction
   requests/responses so bots and interactive agents share one protocol.

## Contract Decisions

- `needs` is the only ordering primitive; YAML order is non-semantic.
- Artifact names form the interface. Paths only locate durable values.
- Source references use `runtime.*`, `context.*`, or
  `<phase>.outputs.<artifact>`.
- Runner and model resolve independently: CLI override, phase override,
  workflow default. The compiled form records the effective phase policy.
- A local executor may serialize a ready set. Managed infrastructure may run
  it concurrently without changing workflow meaning.

## Validation Strategy

- Unit-test malformed graphs and contracts at compiler boundaries.
- Golden-test stable compilation and revisions across YAML key reordering.
- Exercise fan-out/fan-in readiness and failure propagation in local state.
- Exercise managed claim rejection for blocked or unknown phases.
- Verify JSON output against schemas and reject absent/invalid artifacts.
- Compile the bundled template as a release test.

## Constitution Check

The design keeps the CLI dependency-light, preserves deterministic and portable
artifacts, makes local operation first-class, and keeps cloud concurrency an
optional execution capability rather than a semantic requirement.
