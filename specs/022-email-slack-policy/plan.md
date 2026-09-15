# Plan

1. Compile trusted phase constants and policy instructions into the revision bundle.
2. Add named trigger CLI execution and materialize exact trigger input.
3. Extend generic full-access requests with a bound policy decision before auth.
4. Add broker-private identifier projection/resolution, without a Slack adapter.
5. Persist request budget, decision, and effect receipts under the private
   `.outcomeci/.broker/<run-id>` directory, excluded from exported artifacts.
6. Invoke the independent reviewer with no integration capabilities or Vault access.
7. Test the normal local runtime against a stateful Slack simulation with injectable
   primary/reviewer agent invocations, then exercise real local agent invocations.

The earlier composed API fixture stays in its existing worktree as a narrow
HTTP mechanics test; it is not the user-facing workflow or implementation baseline.

Semantic intent authorization is an agent decision. Origin, methods, credential
grants, request budget, identifier isolation, and receipt integrity are deterministic.
