# OutcomeCI CLI

`oci` installs and executes the open-source Standup framework used by OutcomeCI.

```console
pip install outcomeci-cli
oci init
oci validate
oci outcome run --claim /workspace/outcome-claim.json --workspace /workspace
```

Repository context and workflow instructions live in `.outcomeci/`. The same
`outcome.yml` contract can run locally or use OutcomeCI Cloud for managed state,
Digital Twin context, expert messaging, credentials, and runners.

To run the discovery phases with a locally installed Codex or Claude Code:

```console
oci init --backend filesystem
oci outcome compile outcome.yml
oci outcome start "Describe the outcome you want"
oci outcome status
oci outcome continue <run-id> --approve
```

`start` performs intake. Each approved `continue` advances to plan and then
tasks. Local state, artifacts, manifests, transcripts, and usage records are
written beneath `.outcomeci/outcomes/<run-id>/`. Runner and model defaults come
from `outcome.yml`; `--agent` and `--model` provide per-invocation overrides.

`oci init` also installs repository-local Outcome skills for Codex and Claude
Code. They use the current interactive session rather than launching a child
process:

```text
Codex:       $outcome Describe the outcome you want
Claude Code: /outcome Describe the outcome you want
```

The skill coordinates `outcome begin`, `compile`, `validate-artifacts`,
`advance`, and `status`. Managed OutcomeCI runners continue to use `outcome
run --claim ...`; both modes share workflow compilation and artifact schemas.

Filesystem workflows can pin repository evidence explicitly:

```yaml
spec:
  context:
    provider: filesystem
    include:
      - .outcomeci/context/**
      - docs/**
    exclude:
      - node_modules/**
      - dist/**
```

Compiled context records each matched path, size, and SHA-256 hash. Context
changes therefore produce a new workflow revision. Files remain in place and
are read by the active local agent; they are not copied into `outcome.yml`.
