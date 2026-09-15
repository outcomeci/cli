# Tasks: Workspace tunnels

## Setup
- [x] T001 Record contracts and revocation research in contracts/tunnel.md and research.md.

## Foundational
- [x] T002 Validate loopback targets, bounded leases and consumed API grant fields in src/outcomeci/tunnels.py.

## User Story 1
- [x] T003 [US1] Add start/status/stop commands in src/outcomeci/cli.py.
- [x] T004 [US1] Verify pinned frpc archives for Linux/macOS in src/outcomeci/tunnels.py.
- [x] T005 [US1] Supervise frpc with private config and session-specific cleanup in src/outcomeci/tunnels.py.

## User Story 2
- [x] T006 [US2] Prove cancellation, expiry, isolation and installed CLI lifecycle in scripts/verify-local-tunnels.py.

## Polish
- [x] T007 Document local commands and verified limitations in README.md.

## Dependencies
T001 → T002 → T003 → T004 → T005 → T006 → T007. API and CLI follow the same shared contract; per-repo tasks only apply to files owned by that repo.
