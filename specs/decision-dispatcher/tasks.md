# Tasks

- [x] Compiler and explicit dispatcher membership contracts
- [x] Typed decision runtime and asynchronous managed dispatch
- [x] Cloud callback wiring and lineage-preserving trigger envelopes
- [x] Evidence artifacts and normalized token/cache usage
- [x] Compiler/runtime/provider/wire regression tests
- [x] Baseline quality checks: Ruff clean, full suite 599 passed, wheel and sdist built
- [x] Guard LiteLLM 1.104.2's positional OpenAI answer normalization before it can misassign question names
- [x] Final response-guard verification and packaging: 624 tests passed, Ruff clean, wheel and sdist built; cross-repo release verification tracked in the huddle

Validation uses an isolated test environment with the current connectors origin/main snapshot and LiteLLM 1.104.2. Local broker tests require loopback access; test-scoped Git settings disable inherited commit signing for temporary fixture repositories. No production model calls or workflow dispatches occurred.

## Ordinary model BYOK extension

- [x] Shared immutable provider registry and safe model identifiers
- [x] Compiler explicit-key requirements for new providers and fallback profiles
- [x] Preserve OpenAI/Anthropic-only policy review and separate Decisions restrictions
- [x] Fixed local request endpoints/provider selection, no parameter dropping, safe errors
- [x] Bounded per-model output tokens with conservative unknown-model default
- [x] Actual SDK HTTP tests for sixteen providers and typed tool returns where supported
- [x] Final full regression suite and packaging after BYOK extension: 684 tests passed, Ruff clean, wheel and sdist built; provider-error traceback sentinel checks passed
