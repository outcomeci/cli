# Data Model

- **Connection**: logical reference, fixed origin, auth strategy, credential reference, private-network policy.
- **Integration**: reference, connection, access mode, optional OpenAPI source/allowlist, full-access method allowlist.
- **Operation**: capability name, request policy, input schema, response projection.
- **Phase capability grant**: phase and allowed capability name.
- **Execution result**: success, status, projected output, duration, metadata-only audit data.
- **Workflow patch**: parent revision, provenance, reason, and operations to add.
