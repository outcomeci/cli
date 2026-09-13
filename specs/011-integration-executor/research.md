# Research

## Decision: Native executor

Use HTTPX as transport and keep the execution policy in OutcomeCI. Wrapping a test-oriented harness would still require our own credential resolution, authorization, projection, auditing, and lineage layers.

## Decision: Capability names are static; parameters are dynamic

Agents select only capabilities compiled into their current phase. Input remains dynamic within JSON Schema while origin, method, authentication, and exposure are fixed by the workflow.

## Decision: Patch files are proposals

The local CLI writes a new workflow candidate and provenance record. OutcomeCI Cloud remains responsible for assigning immutable version numbers during synchronization.
