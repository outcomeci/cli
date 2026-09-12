# Final platform cleanup

The Outcome runner must execute Codex and Claude through one provider-neutral phase contract and must not assume that OutcomeCI-managed artifact state is a Git repository. A claim explicitly selects an artifact backend. OutcomeCI-managed state is hydrated into the workspace and returned as bounded artifacts; GitHub-backed state remains available only when a repository is explicitly configured.

Acceptance criteria:
- Codex and Claude receive the same normalized execution request and return the same result envelope;
- `artifact_backend.provider` supports `outcomeci` and `github` without implicit repository semantics;
- Git clone, commit, and push occur only for the GitHub backend;
- OutcomeCI backend hydrates supplied workflow, constitution, and prior artifacts and returns bounded artifact contents;
- legacy `state_repository` claims are rejected rather than silently interpreted;
- tests cover both providers and both artifact backends.
