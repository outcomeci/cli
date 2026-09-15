# Feature: Intent-driven, policy-reviewed integration requests

## Goal

A concise outcome.yml passes an email trigger to a Codex or Claude phase that
discovers Slack API calls. No API recipe or provider-specific adapter is required.
An independent agent in the same runner reviews each proposed request. The broker
injects a workflow-granted local Vault credential only after approval.

## Contracts

- The Vault view displays each credential's logical path and provides a one-click copy action for its exact workflow reference (vault:<path>). Copy never reveals or copies credential values, and reports success/failure through notifications.
- Orient this milestone around typed Vault credentials, not a separate workflow-secrets construct. Workflows reference logical credential paths; the broker resolves authorized credentials without exposing values to either agent. Removing the Workflow secrets UI is deferred until this milestone goal is reviewed.
- Every trigger type has a versioned payload JSON Schema enforced before dispatch. email.received defines event ID, timestamp, sender, recipients, subject, text/HTML body, and attachment references with explicit required fields and nullability. Documentation and examples derive from the enforced schemas.
- Every phase declares an explicit type. type: agent has a versioned configuration schema covering instructions, inherited/overridden runner and model, dependencies, literal with values, integration grants, and input/output contracts. Reject unsupported types and invalid configuration before execution.
- Input/output payload contracts use JSON Schemas where structured data is expected; application/json alone is not a type guarantee. Trigger inputs inherit their registered trigger payload schema.
- `spec.agents.orchestrator.instructions` defines the single orchestration agent. Its runner/model inherit agents.default unless explicitly overridden.
- `trigger.<name>` materializes the immutable JSON payload.
- Phase `with` values provide trusted literal configuration such as readable recipients.
- Integration `policy.instructions` compiles a versioned reviewer instruction file.
- Policy runner/model inherit the default agent unless explicitly set on the policy.
- `access.max_requests` bounds all network requests across a durable run.
- `access.opaque_identifiers` replaces provider IDs with broker-owned readable references.
- Review decisions are strict allow/revise/deny objects bound to an exact proposal digest.
- Reviews cannot expand deterministic origin, method, credential, or request-budget limits.
- Agents never receive Vault values, authentication headers, or raw provider identifiers.

## Acceptance

- A simulated email passes through the normal local outcome runtime and a primary
  agent proposes requests; no test directly substitutes a preconfigured Slack recipe.
- Independent review precedes credential resolution and every authenticated effect.
- One DM reaches @izzy using fake Slack users.list, conversations.open, and chat.postMessage.
- Denial, prompt injection, origin escape, invalid decisions, ambiguous recipients,
  provider-level errors, budget exhaustion, and uncertain delivery fail safely.
- Durable receipts prevent replay of confirmed sends and expose uncertain effects.
- A real local agent run uses the same tools as the test harness.

## Milestones

1. Concise YAML and real instructions for review.
2. Local agent/policy/broker execution for review.
3. Simulated Slack end-to-end evidence for review.
