# Implementation Plan: Local Slack CLI Integration

1. Add a focused `outcomeci.slack` module for filesystem generation, workflow registration, Slack CLI discovery, and subprocess delegation.
2. Add nested CLI parsing and JSON status output while allowing Slack's interactive login/install UX to inherit the terminal.
3. Generate `manifest.json` plus `.slack/hooks.json`; the manifest hook prints the exact local manifest through `oci`.
4. Preserve existing user files by default and provide `--force` for intentional regeneration.
5. Test all filesystem and subprocess behavior with a fake command runner.
6. Update the README with the local setup/test journey, build a wheel, and install it into an isolated virtual environment.
7. Add a provider-neutral conversation module that maps Anthropic and OpenAI responses into text or bounded OutcomeCI tool requests.
8. Supply the model with Slack thread history, current run state, and bounded outcome artifacts; validate every requested mutation in the local state machine.
9. Persist auditable conversation and usage records beside the local outcome without persisting credentials.
10. Extend human-hook contracts with readable recipient selectors and explicit wait behavior while preserving existing role-only hooks; keep provider IDs behind the Slack adapter.
11. Add Slack target discovery and workflow assignment commands, plus a provider-neutral durable interaction polling command for agents.
12. Feed resolved and late interaction responses into subsequent phase context and cover blocking, bounded-wait, and continue-while-waiting behavior.
13. Place Codex and Claude Code behind an OS filesystem boundary and mediate runtime human operations through a short-lived run/phase/hook-scoped Unix-socket capability.
14. Re-query Slack before accepting a response so agent-authored content cannot impersonate a human decision.
