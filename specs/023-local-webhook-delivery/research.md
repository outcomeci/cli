Decision: transactional outbox and SQS transport admission into a tenant-scoped durable inbox.
Rationale: SQS has no receive filter; offline local listeners must not burn retries. Dedup and execution fencing remain database-owned.
Alternative: per-workspace/per-version queues multiply resources; holding long-running work in SQS creates visibility and replay hazards.
