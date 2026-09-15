# Local verification

Use the existing dev API, persistent ElasticMQ queue and host UI at :3000. Sync a filesystem workflow declaring webhook.received with delivery: queued, enable its webhook in workflow settings, submit an Idempotency-Key request and run oci workflow listen with the returned workspace/workflow IDs. Acceptance is 202; completion appears only after fenced execution. Current automated evidence is recorded in the workspace huddle. Do not apply Terraform or publish packages from this procedure.
