# Implementation Plan: Cloud Workflow Execution

Extend the existing Outcome Runner image with a `workflow` mode. Hydrate the immutable synced definition and support files into private temporary storage, run the existing local workflow engine, and communicate only through invocation-scoped internal APIs. Extend `oci vault put` to write typed credentials through `/vault/credentials`.

Security boundaries remain the existing broker/policy boundary: only the trusted runner parent receives the invocation-scoped credential lease; agents receive capability tools only.
