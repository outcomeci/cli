# Slack app credential sync

Add `oci integration slack sync-credentials` to bridge installed user-owned Slack
apps in `.outcomeci/integrations/slack` to existing local and cloud Vault contracts.
The user explicitly chooses `--local` or `--cloud WORKSPACE_ID`; local destination
defaults to the app workspace and can be overridden with `--vault-workspace`.
Select one installation by team when ambiguous. Refresh tooling authorization
through Slack CLI, retrieve the selected installed app token using installed
scopes, verify app/workspace/bot identity, and store without exposing secrets.
Repeated local syncs replace the entry; repeated cloud syncs rotate the existing
generic secret, preserving grants unless explicitly supplied. No implicit grants,
connection rewrites, Slack messages, or access expansion.

Validation covers encrypted persistence, repeat sync, cloud rotation/grants,
installation ambiguity, identity mismatch, provider failures, and secret redaction.
Slack installation token retrieval uses the upstream CLI's apps.developerInstall
API and is therefore an explicit provider compatibility dependency.
