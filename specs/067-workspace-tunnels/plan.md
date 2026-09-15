# Implementation Plan: Workspace tunnels

**Date**: 2026-09-15 | **Spec**: [spec.md](spec.md)

## Summary
API persists expiring session grants; private frp plugin admits exactly the assigned HTTP proxy. A Go HTTP gateway checks grants before forwarding and cancels live requests when authorization ends. CLI supervises matching frpc with a private configuration.

## Technical Context
Python 3.11+ CLI; FastAPI/asyncpg/aiosql API; Go stdlib gateway; frp 0.71.0; PostgreSQL; pytest and live local HTTP proof. Local development only, one frps. Reject HTTP upgrades.

## Constitution Check
Typed HTTP models and aiosql-only new persistence. Migration is additive and reversible by removing only tunnel sessions. Every API route added to auth catalog, including private service endpoint; ordinary valid user JWTs cannot authorize tunnels through it. No DB reset, deploy or publication.

## Project Structure
API: api/app/tunnels.py, api/app/routers/traces/tunnels.py, api/db/queries/tunnels.sql, api/db/migrations/210_workspace_tunnels.sql, tunnel/gateway, tunnel/Dockerfile.
CLI: src/outcomeci/tunnels.py, src/outcomeci/cli.py, tests/test_tunnels.py.

## Sequencing
Settle revocation contract, implement API/container first, then CLI, then live proofs. No remote work is necessary.
