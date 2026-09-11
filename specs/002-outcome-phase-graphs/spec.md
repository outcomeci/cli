# Feature Specification: Outcome Phase Graphs

## Goal

Allow an `outcome.yml` author to define one named orchestration method and a
custom directed acyclic graph of outcome phases. The same compiled workflow
must explain what may run, what may run together, and which durable artifacts
each phase consumes and produces.

## Requirements

- Require exactly one entry beneath `instructions`; treat its key as the
  orchestrator command name, resolve its Markdown `path` beneath `.outcomeci/`,
  and accept optional orchestrator `runner` and `model` overrides.
- Replace the fixed phase allowlist with validated user-defined phase IDs.
- Require each phase to declare an instruction path, dependency list, expected
  inputs, and expected outputs.
- Interpret `needs` as the sole ordering primitive and compile deterministic
  topological execution levels.
- Let phases at the same topological level be eligible in parallel while
  preserving identical artifact semantics for a serial local executor.
- Resolve each phase's runner and model independently from an explicit phase
  value and then `agents.default`, so one phase may override either or both.
- Give every input and output a logical name. Inputs reference a runtime,
  context, or dependency output source; outputs declare path and media type.
- Accept optional JSON Schema references beneath `.outcomeci/` for structured
  inputs and outputs and include their contents in the workflow revision.
- Reject cycles, invalid identifiers, unknown or self dependencies, unsafe or
  duplicate paths, missing producers, ambiguous producers, and dependency
  violations.
- Hash normalized topology, contracts, runner/model policy, instruction
  contents, and filesystem context into the workflow revision.
- Validate required output artifacts, media types, and declared JSON Schemas
  before completing a phase or unblocking its dependants.
- Keep the bundled Standup code workflow as the `oci init` default using the
  generalized schema.
- Support phase-local human participation before, during, and after agent work.
  Persist every local request and response as part of the outcome trajectory.
- Allow a running agent to invoke a declared during-phase interaction and let a
  local user answer through the CLI before execution resumes.

## Acceptance Criteria

- A workflow may rename `standup` to another orchestrator and compile with one
  referenced Markdown instruction file.
- Zero or multiple orchestrator entries fail with a precise field error.
- A sequential graph compiles into one phase per execution level.
- A fan-out/fan-in graph compiles independent phases into the same level and
  the join only after all dependencies.
- Reordering YAML mappings does not change the workflow revision or graph.
- A phase cannot complete until every declared output exists within the run
  artifact directory and satisfies its declared media type.
- Local status identifies all currently ready, running, blocked, completed,
  and failed phases without inventing a single current phase.
- Existing managed claim execution can select one eligible phase without
  trusting an uncompiled phase name from the claim.
- Required before/after interactions block execution or downstream release;
  consultation and review responses are available to the resumed phase.
- Per-phase runner/model overrides inherit missing values from the default and
  the resolved policy appears in compiled output.

## Out of Scope

- A generic distributed scheduler inside the CLI.
- Arbitrary shell commands as phase instructions.
- Conditional expressions, loops, retries, or dynamic phase creation.
