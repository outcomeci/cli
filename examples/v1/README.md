# outcomeci.com/v1 examples

Two workflows written in the `outcomeci.com/v1` format. `oci` compiles them
like any workflow file, and `oci workflow debug --image` runs them in the
runner container against a cloud workspace's vault.

| File | What it does |
| --- | --- |
| `slack-to-github-pr.outcome.yaml` | A feature request in Slack (an @mention or a DM) becomes a discussed plan, then one pull request per repository it names. |
| `sentry-to-github-pr.outcome.yaml` | A Sentry alert is triaged in #sentry; a thumbs-up reaction approves a fix PR. |

Each file is one workflow, named `{workflow}.outcome.yaml`. Long `reason:`
instructions live in `.outcomeci/instructions/`; short ones stay inline.

## Reading a v1 file

- `secrets`, `apis` and `reasoning` are the three things a step may touch.
  Secrets are vault references and only an API binding can use one. `uses:`
  names a provider from `outcomeci/connectors`, which defines its operations.
- `steps` run top to bottom. Each key under a step's `returns:` is one of its
  outputs, and `<step>.<output>` reads it, so `triage.decision` is the
  `decision` the triage step returned. `<step>.calls.<api>.<operation>` reads
  the last call of that operation the runtime recorded for the step; a grant
  can name a call with `as`, as in `slack.post: {channel: build, as: plan_post}`,
  read as `draft.calls.plan_post`.
- `can:` is the step's grant. Mechanical rules are grant arguments, such as
  `slack.post: {channel: build}` or `github.write: {repo: discuss.plan.repo}`,
  enforced on every call; rules a grant cannot express go in `policy:`,
  checked by the policy reviewer before each call that changes something.
  Arguments can come from data, so `slack.post: {channel: trigger.channel}`
  replies wherever the request came from and nowhere else.
- `when:` skips a step unless its condition holds. A step that reads a skipped
  step is skipped too.
- `for_each: <list> as <name>` runs the step once per item, each run with its
  own grant, such as `github.write: {repo: target.repo}`; the step's outputs
  become lists, one entry per item.
- `await:` waits for a single human signal, such as a reaction, for up to
  `timeout:`. If the window closes without it, every remaining step is skipped.
- `converse:` discusses a plan in a thread, one fresh agent turn per reply,
  until the requester approves it or `max_turns` is reached. The step returns
  the latest `plan` and a `status` of `converged`, `capped` or `timed_out`,
  and every turn and plan version is kept in the step's `consultation.json`.
  The runtime posts each plan version itself, so the thread shows exactly what
  the next steps receive.
- `by:` on `await` or `converse` counts only one person's signal, such as
  `by: trigger.user` for whoever made the request. Before an `await` waits,
  the runtime posts what approving lets the later steps do.
