# Contributing to OutcomeCI CLI

OutcomeCI CLI supports Python 3.11 and newer. Keep changes focused, preserve existing command
contracts unless the change explicitly calls for a breaking release, and add tests for observable
behavior.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

## Quality checks

Run the same checks enforced in CI before opening a pull request:

```bash
ruff format --check src tests
ruff check src tests
pytest -q
python -m build
```

Use `ruff format src tests` to apply the repository's canonical formatting.

Commit messages follow Conventional Commits because releases are generated from commit history.

## Source map

`src/outcomeci/cli.py` defines the `oci` commands and dispatches to the packages below.
Package `__init__.py` files only describe their purpose; import the module you need directly.

| Package | Responsibility | Start reading here |
| --- | --- | --- |
| [workflow/](src/outcomeci/workflow/) | Read, validate and compile workflows; schemas, diagrams, starter templates and publication | `compiler.py`, then `language.py` |
| [runtime/](src/outcomeci/runtime/) | Launch containers and agents, execute steps, checkpoint human waits and prepare repository checkouts | `launcher.py` → `container.py` → `engine.py`; `steps.py` implements runtime-driven steps |
| [broker/](src/outcomeci/broker/) | The agent's API security boundary: capabilities, grants, policy review and authentication | `server.py` → `policy.py` → `executor.py` → `auth.py` |
| [vault/](src/outcomeci/vault/) | Typed credentials, encrypted local storage, leased credentials and rotation | `credentials.py`, `local.py`, `leases.py` |
| [reasoning/](src/outcomeci/reasoning/) | Model tool loops, typed decisions, provider settings and capability checks | `models.py`, `decisions.py`; `providers.py` and `capabilities.py` are shared with the API |
| [artifacts/](src/outcomeci/artifacts/) | Persist and export run evidence: manifests, calls, policy decisions and transcripts | `records.py`, `manifest.py`, `transcripts.py`, `dataset.py` |
| [cloud_runner/](src/outcomeci/cloud_runner/) | Managed job authorization, execution, checkpoints and agent adapters | `bootstrap.py`, `main.py`, `providers/` |

The remaining top-level modules are `cloud.py` (the CLI's Cloud API client),
`mcp_setup.py` (coding-agent registration), and `security.py` (private-path filtering
and atomic writes). `schemas/` contains the packaged trigger JSON schemas.

## Following a run

For a local run, start at `cli.py`, follow `runtime/launcher.py` into
`runtime/container.py`, then read `runtime/engine.py`. The compiler in
`workflow/compiler.py` validates and lowers the workflow through `workflow/language.py`.
Agent steps invoke a subprocess; model steps use `reasoning/models.py`; waits,
conversations and dispatch steps are handled by `runtime/steps.py`.

Agents call capabilities through `broker/server.py`. `broker/policy.py` checks grants,
budgets and required review before `broker/executor.py` resolves credentials and sends
HTTP requests. `broker/auth.py` applies connector authentication. Changes to this path
should preserve credential isolation and durable write receipts.

Managed runs enter through `cloud_runner/bootstrap.py` and `cloud_runner/main.py` and
reuse the same compiler and execution engine. Their API wire contracts live in
`cloud_runner/models.py` and `cloud_runner/client.py`; these are distinct from the
model reasoning code in `reasoning/`.

## Tests and downstream consumers

Tests live in `tests/`, with workflow fixtures in `tests/examples/`. Compiler tests
start in `test_config.py` and `test_v1_compiler.py`; execution and security coverage
includes `test_local.py`, `test_v1_runtime.py`, `test_policy_execution.py` and
`test_integrations.py`. The `test_cloud_runner_*` files cover the managed runner.

The OutcomeCI API imports compiler, broker, reasoning and runtime modules directly.
When moving shared modules, update those API imports and its pinned CLI revision in
the same release. The source layout has no aliases for the former flat import paths.
The container module entrypoint is `python -m outcomeci.runtime.container`; the managed
image entrypoint remains `python -m outcomeci.cloud_runner.bootstrap`.
