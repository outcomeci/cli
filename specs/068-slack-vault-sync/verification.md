# Verification

- Full CLI suite: 191 passed with local networking enabled; restricted sandbox
  broker HTTP test requires the same network permission as existing tests.
- Ruff and diff whitespace checks pass.
- Installed the local build and successfully synced the existing Outcome Bizzot
  installation in `/home/ike/companies/brickbuds` to the encrypted local Vault in
  `/home/ike/companies/sparepartslabs`, logical path `slack/bot-token`.
- Slack CLI refreshed its expired tooling authorization. Remote installed
  manifest export, selected-app bot token retrieval, and bot identity validation
  all succeeded. No Slack message was sent.
- Cloud create, rotation, grants preservation/replacement, and error redaction
  verified using mocked API boundaries; no real cloud credential was written.
