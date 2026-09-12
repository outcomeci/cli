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
