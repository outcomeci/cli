# Feature Specification: Credential-blind integration executor

## User Scenarios & Testing

### User Story 1 - Execute approved API capabilities (Priority: P1)

A workflow author grants an agent named API capabilities for a phase. The agent supplies non-secret input and receives only the declared output fields.

**Independent Test**: Execute a declared operation against a fixture API and verify the fixture receives authentication while command output and audit data contain no credential.

### User Story 2 - Configure APIs at the right trust level (Priority: P1)

A workflow author can declare a strict operation, import allowlisted operations from an API specification, or intentionally allow dynamic relative requests against one fixed origin.

**Independent Test**: Compile one workflow for each mode and verify undeclared origins, methods, and operation IDs are rejected.

### User Story 3 - Turn discovery into lineage (Priority: P2)

After a broad-access run discovers a useful request, the user can produce a constrained workflow patch. Applying it checks the expected parent revision and writes a new file for synchronization as an immutable child version.

**Independent Test**: Propose and apply a patch, then verify provenance and parent revision are preserved and a stale-parent apply is rejected.

## Requirements

- **FR-001**: The compiler MUST validate connections, integrations, declared operations, phase capabilities, access modes, fixed origins, allowed methods, input schemas, and response projections.
- **FR-002**: Runtime callers MUST NOT provide credential material, absolute destination URLs, response exposure rules, or undeclared HTTP methods.
- **FR-003**: The executor MUST support no-auth, API key header/query, Basic, bearer, OAuth 2 client credentials, and OIDC discovery with client credentials.
- **FR-004**: The executor MUST validate dynamic input before network activity and encode path, query, header, and JSON-body values by context.
- **FR-005**: The executor MUST reject loopback, link-local, private, multicast, and otherwise unsafe resolved destinations unless the connection explicitly opts into private networking.
- **FR-006**: Results MUST contain only status, declared projected output, duration, and a credential-free audit envelope.
- **FR-007**: Learned-operation patches MUST declare their parent revision and provenance and MUST NOT modify the source workflow in place.
- **FR-008**: Patch application MUST use optimistic concurrency and emit a distinct child workflow document suitable for cloud version synchronization.
- **FR-009**: Existing Slack and custom human connections MUST remain compatible.

## Success Criteria

- **SC-001**: Tests demonstrate zero credential bytes in successful results, errors, audit envelopes, and compiled workflows.
- **SC-002**: Every attempted origin or method escape is rejected before sending a request.
- **SC-003**: All three access modes compile deterministically and yield stable revision digests.
- **SC-004**: A patch cannot apply when its parent parent revision differs from the current compiled revision.

## Assumptions

- Cloud workflow revisions and Vault leases already exist and remain the production source for immutable versions and scoped credentials.
- OpenAPI import is initially compilation/discovery metadata; network fetching and caching will be added after the local declared-operation executor is stable.
