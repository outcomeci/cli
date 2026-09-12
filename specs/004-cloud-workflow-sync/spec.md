# Feature Specification: Cloud Workflow Sync

## User Scenarios & Testing

### User Story 1 - Sign in from the CLI

An approved OutcomeCI user runs `oci auth login`, opens the displayed browser URL, approves the request, and returns to an authenticated CLI without copying credentials.

### User Story 2 - Synchronize a workflow

An authenticated user validates and uploads an OutcomeCI YAML or JSON workflow to a workspace, explicitly choosing whether it creates a workflow or versions one.

## Requirements

- The CLI must implement browser-assisted login, status, and logout.
- Credentials must be stored outside the working repository with owner-only permissions.
- The CLI must validate workflow content before upload.
- Sync must require an explicit create/version intent and provide actionable conflicts.
- API base URL and browser application URL must be configurable for local development.

## Success Criteria

- A new user can authenticate and upload a valid workflow without copying a token.
- Invalid files never reach cloud persistence.
- Repeating create cannot silently overwrite a workflow.
- A version upload produces a new immutable revision.

## Assumptions

- `outcome.yml` and equivalent JSON representations use the current OutcomeCI workflow schema.
