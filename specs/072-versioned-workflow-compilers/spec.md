# Feature Specification: Versioned Workflow Compilers

## Goal

Make workflow `apiVersion` the explicit compiler compatibility boundary so old workflows retain their original semantics.

## Requirements

- Resolve compilation through a registry keyed by `apiVersion`.
- Retain the v1alpha1 compiler as a frozen entry.
- Reject unknown versions with a clear configuration error.
- Make the selected contract version visible in compiled output.

## Acceptance Criteria

- Existing v1alpha1 fixtures compile unchanged.
- Unknown versions fail closed.
- Compiler output identifies the selected API and engine versions.
