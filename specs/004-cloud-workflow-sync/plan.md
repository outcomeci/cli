# Plan: Cloud Workflow Sync

- Add a small Cloud client with device polling, token refresh, and protected credential storage.
- Add `oci auth login|status|logout` and `oci workflow sync` commands.
- Reuse the existing compiler for YAML and validate JSON by parsing its workflow document through the same contract.
- Cover pending authorization, refresh, create/version payloads, and local validation in tests.
