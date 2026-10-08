# Scan for the company account

You are finding recent public X posts that the company account, `@outcomeci`,
could reply to: people asking which tool to use to run agent workflows,
comparing ways to give an agent scoped access to GitHub or Slack, describing a
workflow they want to automate, sharing what they automate with coding agents,
asking how others run agents unattended, or mentioning the company by name.
The topics to search are: "agent workflows", "Claude Code automation", "Codex
CLI automation", "AI agents running unattended", "cron jobs for AI agents", and
"giving an AI agent API access safely".

All searches use `x_company.search_recent`.

## 1. Find conversations we already joined

First, make exactly one search with query `from:outcomeci is:reply` and
`max_results` 100. These are the company's own replies from the last seven
days. Collect the `conversation_id` of every post in the result into a set of
conversations we have already joined. If this search fails or returns an
error, stop and return no posts: never risk offering a post we already replied
to.

## 2. Find candidates

Then make at most 8 more searches, each with `max_results` 30. Spend one on
mentions of the company name and handle, and the rest on the topics in X
recent-search syntax, varying the queries rather than repeating one with small
changes. Always append `-is:retweet -is:reply lang:en -from:outcomeci
-from:ike4est` to these searches so our own posts, retweets, and reply chains
are excluded.

Drop every post whose `id` or `conversation_id` is in the set from step 1: the
company has already replied in that conversation. Count how many distinct
posts you dropped this way and return the count as `already_replied`.

## 3. Return

Return at most 30 distinct remaining posts, the ones that best fit the kind of
post described above; leave out the rest. For each post give its `id`, its
`url` as `https://x.com/i/status/<id>`, the author's username and
`author_followers` from the `authors` list in the result, `posted_at`, the
`text` cut to its first 160 characters, `likes` and `replies` from the post's
public metrics, and the `query` that found it. Keep the result compact: 30
posts at most, nothing beyond the fields named here, no commentary. Do not
judge or rank the posts here, and do not post anything.
