# Tasks: Outcome Phase Graphs

## Phase 1: Compiler Foundation

- [x] T001 Add JSON Schema validation dependency and packaging metadata in `pyproject.toml`
- [x] T002 Define orchestrator, phase, dependency, agent-policy, and named artifact contract parsing in `src/outcomeci/config.py`
- [x] T003 Implement safe instruction/schema path resolution and referenced-content hashing in `src/outcomeci/config.py`
- [x] T004 Implement graph validation and deterministic topological levels in `src/outcomeci/config.py`
- [x] T005 Implement independent runner/model inheritance and emit effective policy per phase in `src/outcomeci/config.py`

## Phase 2: User Story 1 — Compile a Custom Outcome Workflow

**Independent test**: Compile sequential and fan-out/fan-in workflows and receive stable normalized graphs regardless of YAML key order.

- [x] T006 [P] [US1] Add valid custom graph and policy inheritance fixtures in `tests/test_manifest.py`
- [x] T007 [P] [US1] Add invalid orchestrator, dependency, source, path, and cycle cases in `tests/test_manifest.py`
- [x] T008 [US1] Return topological levels, named contracts, resolved instructions/schemas, and effective policies from `compile_workflow` in `src/outcomeci/config.py`
- [x] T009 [US1] Update `oci outcome compile` rendering for graph and contract output in `src/outcomeci/cli.py`

## Phase 3: User Story 2 — Execute Ready Phases Locally

**Independent test**: Run intake, observe two review phases ready together, complete both serially, and observe the join phase become ready.

- [x] T010 [P] [US2] Add fan-out/fan-in local state and artifact-validation tests in `tests/test_local.py`
- [x] T011 [US2] Replace the fixed local phase cursor with per-phase lifecycle state and ready-set derivation in `src/outcomeci/local.py`
- [x] T012 [US2] Resolve declared inputs and build bounded phase prompts in `src/outcomeci/local.py`
- [x] T013 [US2] Validate required output existence, media type, and JSON Schema before phase completion in `src/outcomeci/local.py`
- [x] T014 [US2] Update local start, continue, and status commands for multiple ready phases in `src/outcomeci/cli.py`

## Phase 4: User Story 3 — Execute a Managed Eligible Phase

**Independent test**: A managed claim can run a ready phase and cannot run an unknown or dependency-blocked phase.

- [x] T015 [P] [US3] Add eligible, blocked, failed-dependency, and policy-resolution claim cases in `tests/test_outcome.py`
- [x] T016 [US3] Validate claimed phase eligibility against the compiled graph in `src/outcomeci/outcome.py`
- [x] T017 [US3] Feed only declared inputs and effective agent policy into managed phase invocation in `src/outcomeci/outcome.py`
- [x] T018 [US3] Validate and record named managed outputs, transcripts, and usage before completion in `src/outcomeci/outcome.py`

## Phase 5: Default Workflow and Release Safety

- [x] T019 Update the generated Standup code workflow and instruction assets to the generalized contract in `src/outcomeci/templates.py`
- [x] T020 [P] Add init/compile regression coverage for the bundled workflow in `tests/test_cli.py`
- [x] T021 [P] Update manifest graph, artifact, and effective-policy recording tests in `tests/test_manifest.py`
- [x] T022 Run the full CLI test suite and build a wheel using the commands documented in `README.md`

## Phase 6: Human Participation

- [x] T023 Parse and compile before, during, and after human interaction contracts in `src/outcomeci/config.py`
- [x] T024 Persist local interaction requests and responses with phase state in `src/outcomeci/local.py`
- [x] T025 Add agent-invokable request and user response commands in `src/outcomeci/cli.py`
- [x] T026 Add requester confirmation to the generated workflow in `src/outcomeci/templates.py`
- [x] T027 Add durable interaction lifecycle coverage in `tests/test_local.py`

## Dependencies

- T001–T005 block every user story.
- US1 blocks US2 and US3 because execution consumes compiled graphs.
- US2 and US3 may proceed in parallel after US1.
- T019–T022 follow the runtime work so the default exercises the released contract.

## Suggested MVP

Complete T001–T014: users can author, compile, and locally execute deterministic
custom workflows with per-phase model selection, parallel-safe dependency
levels, and validated handoff artifacts.
