# Plan

API: typed bounded heartbeat events, existing lease validation, idempotent aiosql
insert into invocation events, and phase/level projection in existing reader.
CLI: durable journal events and advisor result preservation, callback to identify
created local run, negotiated heartbeat batches and final drain. Existing portal
renders events and metadata; no portal changes required.
