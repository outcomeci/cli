# Feature Specification: Workspace tunnels

**Feature Branch**: `feat/workspace-tunnels`
**Created**: 2026-09-15
**Status**: Ready
**Input**: Expose explicitly approved local HTTP services through workspace-scoped revocable tunnels.

## User Scenarios & Testing

### User Story 1 - Start a scoped tunnel (Priority: P1)
An owner starts a tunnel to a loopback HTTP port and receives a unique address.
**Independent Test**: One workspace forwards a request; another cannot reuse its credential or hostname.
**Acceptance Scenarios**:
1. Given an authenticated owner, when a session starts, then only its assigned hostname forwards.
2. Given a non-owner or read-only key, when a session starts, then access is denied.

### User Story 2 - Stop exposure (Priority: P1)
Owners revoke a tunnel or let its lease expire.
**Independent Test**: Revoking a streaming request stops it, and reconnects are rejected.
**Acceptance Scenarios**:
1. Given live traffic, when revoked or expired, then new traffic is denied and active requests terminate within five seconds.

### Edge Cases
- Authorization outage fails closed; no cached grants.
- A reconnect cannot change hostname or proxy type.
- WebSocket upgrades are rejected in this HTTP-only milestone.
- CLI interruption revokes the lease; crashed clients leave only a bounded lease.

## Requirements

### Functional Requirements
- **FR-001**: Sessions MUST be workspace-owner scoped and reject read-only keys.
- **FR-002**: Credentials MUST remain private, hashed in storage, and absent from status responses and logs.
- **FR-003**: Expiry and revocation MUST reject new traffic and stop existing requests.
- **FR-004**: Only one assigned HTTP hostname and explicit loopback target MUST be permitted.
- **FR-005**: Matching client/server versions MUST be pinned and verified.
- **FR-006**: SQS async ingress MUST remain independent and unchanged.

### Key Entities
- Tunnel session: workspace, owner, high-entropy hostname, target port, expiry, revocation and last connection observation.

## Success Criteria
- **SC-001**: All cross-workspace and hostname substitution proofs reject access.
- **SC-002**: Live request cancellation is observed within five seconds of revocation.
- **SC-003**: Healthy HTTP requests pass; unavailable authorization denies them.

## Assumptions
- First slice is local, single-server and HTTP-only; public environment deployment and UI controls follow separately.
