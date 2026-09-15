# Research

- Decision: frp 0.71.0, matching verified client/server release. Source: https://github.com/fatedier/frp/releases/tag/v0.71.0
- Stock frps has no admin client-close endpoint. Ping errors close stock frpc; heartbeat timeout eventually closes noncompliant clients. HTTP NewUserConn hooks are unsupported. Source: https://github.com/fatedier/frp/blob/v0.71.0/server/control.go
- Decision: authorization-gated HTTP gateway cancels open requests on lease invalidation. No WebSockets in first slice. This avoids modifying frps or claiming heartbeat alone revokes keepalive HTTP traffic.
- Alternative: custom frps control operation, rejected to keep stock upstream binaries.
