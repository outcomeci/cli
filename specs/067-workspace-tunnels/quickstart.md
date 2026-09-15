# Local verification

Build the tunnel container, configure private API/service authentication, start local frps and gateway, then use the branch CLI to expose an explicit loopback HTTP port. Verify a request with the assigned Host header, reject cross-workspace credential/hostname substitution, revoke an open streaming request, and confirm expiry/reconnect failure. No public deploy is required.
