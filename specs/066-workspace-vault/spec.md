# Feature: Vault CLI

## Objective

Expose a single provider-neutral `oci vault` namespace for workspace credential administration and workflow grants. All managed credentials travel through the OutcomeCI Vault API; provider commands do not create independent credential stores.

## Required contracts

- `oci vault list|get|put|rotate|revoke` operates on stable workspace-local paths.
- `oci vault grant|ungrant` manages workflow capabilities.
- Commands use `oci auth` only to authenticate to the API and never print secret values.
- Machine-readable JSON is the default stable output contract.

## Acceptance criteria

- Static secrets can be created, rotated, listed, and revoked through the cloud API.
- Workflow grants can be added and removed.
- Errors preserve API authorization/not-found/conflict meaning without echoing submitted values.
- Tests prove secret values never appear in stdout, stderr, or persisted CLI configuration.
