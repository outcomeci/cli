# Implementation plan

Compile webhook.received as an async-only typed trigger. Authenticate an outbound listener through existing Cloud credentials, match YAML/support digests, and claim scoped inbox entries. Start effects only after lease acceptance; maintain heartbeat and private durable receipts. Refuse replay of started or uncertain work. Retry transient registration/claim transport failures, not permissions or effects. Remove loopback forwarding and response arguments. Local credentials remain private to the existing broker.
