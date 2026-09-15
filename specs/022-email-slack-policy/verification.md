# Local milestone verification

Verified 2026-09-15. This closes the local schema/policy/Vault/access milestone;
cloud email dispatch is a separate follow-up and is not claimed here.

## Results

| Check | Result |
| --- | --- |
| CLI regression suite | 154 passed |
| CLI source/tests/new proof scripts | Ruff lint and formatting passed |
| API auth, workspace Vault, OpenCode access | 341 passed |
| UI regression suite | 156 passed |
| UI TypeScript | Passed |
| Documentation links | 26 pages, 34 links resolved |
| Real bubblewrap filesystem/DNS proof | Passed |
| Real host Codex + independent policy reviewer | Passed: 3 reviewed requests, exactly 1 simulated message |
| Installed CLI inside rebuilt outcome container | Passed: 3 reviewed requests, exactly 1 simulated message; typed output completed |
| API/UI health | HTTP 200 on ports 8000/3000 |

Host proof: `20260915135320621395-receipt-arrived`, retained at
`/tmp/oci-email-proof-_rygo3m2`.

Container proof: `20260915135825559083-receipt-arrived`. Disposable container
storage was removed after success. Its output was captured by the local test
command. Image: `outcomeci-outcome:milestone-local`, SHA-256
`620266a65c6d7ce4b2c0a8c3df70030b906da93435d76fa99d14322343848f0e`.
The development image builds and installs a wheel from current local CLI source;
its development package version is `0.0.0`.

The separately rebuilt wheel is installed only in `cli/.venv`, with version
`0.11.1.post1.dev3+gc6786daf2.d20260915`. No public package, container or global
CLI installation was changed.

Vault visual QA screenshots are retained in `/tmp/outcomeci-vault-qa/`:
desktop/mobile, light/dark, all four credential forms. Browser API responses were
mocked; these screenshots do not claim real provider authorization.

## Security and durability checks

- Restricted runtime mounts instead of exposing the host filesystem.
- DNS works through chained resolver symlinks without exposing all of `/run`.
- Environment files, Vault storage/key, private broker maps and SSH storage are
  inaccessible to the primary agent.
- Symlinked instruction files cannot read a private Vault or broker path.
- Broker and primary agent use the same compiled workflow snapshot.
- Policy decisions bind the proposal digest and fail closed; deterministic
  capability/origin/method/budget restrictions remain authoritative.
- Secret/header values are redacted before API responses reach an agent.
- Provider references are private, integration-scoped and ambiguity fails closed.
- Confirmed duplicate requests return saved receipts; uncertain effects are not
  automatically replayed. Private journals are excluded from synced artifacts.
- OpenCode is default-off and requestable, with backend enforcement.

## Reproduce

From `cli/`, using the local development environment:

```sh
.venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python scripts/verify-agent-sandbox.py
PYTHONPATH=src .venv/bin/python scripts/verify-email-policy.py ..
```

The agent proof needs an existing Codex login. It copies the root workflow and
instructions into disposable storage, uses a synthetic local Vault token and a
Slack-shaped HTTP server, and preserves the intent-driven phase rather than
precomposing the API requests. No real Slack messages are sent. Only the real
Codex path was exercised end-to-end; Claude invocation is covered by regression
tests, not a real Claude session in this verification.

Docker's default seccomp/AppArmor/inherited `/proc` masks prevented nested
bubblewrap on this host. The disposable container proof used host networking,
UID 1000 and `seccomp=unconfined`, `apparmor=unconfined`,
`systempaths=unconfined`; it granted no host-level capabilities. Codex auth was
mounted read-only and copied into ephemeral storage. These are local proof
settings, not an approved production isolation configuration. Unsupported
runtime sandboxing fails closed. Review cloud runtime isolation before rollout.

The broader API suite previously showed three unrelated main-branch failures
in fixture/trace-route/member-query tests. The targeted milestone checks above
are green; this document does not claim the entire API suite is green.
