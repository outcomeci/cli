# Tunnel contract

API produces POST/GET/DELETE /v1/workspaces/{workspace_id}/tunnel. POST body: local_port, ttl_seconds; response session plus credential and frp server host/port. GET omits credential. Owner and write-capable credentials only.

Private POST /v1/internal/tunnels/authorize consumes separate server service bearer, hostname, optional session credential. Missing credential is only for public HTTP hostname checks by the trusted gateway. Response: active boolean. Login and NewProxy must provide credential. No cached positive authorization. Revoked/expired/unavailable is denied.

Gateway rejects upgrades, imposes request limits, bounds active cancellation to five seconds. Plugin only permits HTTP, assigned custom_domains, exactly named session proxy and no subdomain override. CLI enforces loopback HTTP port; frps cannot attest a malicious client target.
