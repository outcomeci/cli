# Data model

Session UUID, workspace FK, owner UUID FK, hostname unique, local_port, credential_hash, expires_at, revoked_at, last_seen_at, created_at. One unrevoked session per workspace. Creation locks workspace, expires old grant and returns new credential once. Reconnect updates observation but never extends expiry. Revocation is idempotent.
