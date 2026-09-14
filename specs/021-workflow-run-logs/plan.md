# Plan: Cloud Runner Lifecycle Logs

1. Extend the internal client with log delivery.
2. Add a best-effort lifecycle emitter around claim, agent execution, completion, and failure.
3. Test sequencing, payload bounds, and unavailable-API behavior.
