Ingress: POST webhook returns 202 after atomic commit. No forwarded responses.
Transport: outcomeci.workflow.dispatch/v1 with invocation_id, workspace_id, workflow_id, workflow_revision_id UUID/string references only.
Runtime: authenticated register/claim/start/heartbeat/complete. Claim returns typed trigger input and lease, scoped to exact synced YAML/support digests. Email/webhook share the contract. SQS receipt is deleted only after durable inbox admission.
