Reply once in the triage thread, in Slack mrkdwn, one block per repository in
the plan's order, blank line between blocks:

✅ *Fix opened*  ·  `owner/repo`
<pr_url|#<number> · <PR title>>  ·  branch `<branch>`
<One line: what changed and how it was verified.>

For a repository in `fix.skipped`:

⏭ *Skipped*  ·  `owner/repo`
<the reason, one line>

When the plan spans several repositories, end with one line:
*Merge order*  `owner/repo-a` → `owner/repo-b`

Use `*bold*`, backticks and `<url|label>` links only; no Markdown headings,
`**`, or `[text](url)`. Keep it under 1,500 characters.
