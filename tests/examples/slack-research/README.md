# Slack research test

This ordinary child workflow belongs to `slack-dispatcher`. Its Jev decision
chooses official documentation, news, or general web; only the selected model
step searches Tavily and posts one cited answer to the originating Slack thread.

Requires the Tavily connector, ordinary-workflow decision support, and the
managed connector-call API. The implementation branches must be released before
this definition can run on a deployed environment.

## Credentials

The example uses the existing staging paths `slack/bot-token` and
`typesafeai/outcomeci-stg`; grant both to this child. Its answer model uses the
managed OpenAI key. `apis.search: {uses: tavily}` uses the API's configured
platform Tavily key. To test BYOK, declare `tavily: vault:tavily/api-key` under
secrets and set `auth: secrets.tavily` on the search binding, then grant that
credential to the child. A missing/invalid user key never falls back to platform.

Managed searches record the actual provider credit count with the workspace,
workflow, run, step, and platform/BYOK source. Missing usage remains unknown;
this feature does not introduce a monetary charge. Local BYOK searches retain
usage in their broker journal but are not added to managed workspace accounting.

## Staging test cases

Mention the existing staging Slack app, or send it a DM:

| Request | Dispatcher result | Research source |
| --- | --- | --- |
| How does Python asyncio.TaskGroup handle failures? | research | official_docs |
| What did Reuters report today about renewable energy? | research | news |
| Why are octopuses able to change color? | research | general |
| Add a health endpoint to `your-org/test-repo` and open a PR. | github_pr | not run |
| Hello | none | not run |

For each research run check `choose_source/decision.json`, the selected
`answer_*` step, and the broker search receipt. Verify the actual topic and
domain filters, one Slack reply with real source URLs, and one usage row per
provider call. Repeating an API delivery with the same receipt must return the
original result without a second provider call. A fresh search gets a new receipt.

The GitHub route retains its human plan-approval gate. Do not approve a test PR
unless its changes are wanted. The `none` route intentionally posts nothing.

Automated fixture tests in CLI `tests/test_slack_research.py` cover all three
research branches with controlled providers and no Slack/network side effects.
Live Jev classification and real provider credits must be checked after staging
has the matching release; the controlled tests do not establish model accuracy.
