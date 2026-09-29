# Quickstart

1. Add an HTTP connection, integration, operation, and phase capability to `outcome.yml`.
2. Run `oci validate`.
3. Inspect the phase surface with `oci integration list --phase intake`.
4. Execute using JSON from stdin: `printf '%s' '{"title":"Example"}' | oci integration execute linear.create_issue --input-stdin`.
5. Confirm output contains only fields declared under `response.expose`.
6. Propose a learned operation with `oci integration patch propose ...`, then apply it to a new file with the current parent revision.
