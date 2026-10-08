# Choose what @outcomeci engages with

You receive the posts the scan found, as `scan.posts`. Drop duplicates by `id`
and pick at most 10 posts worth a reply from the company account, `@outcomeci`.

Keep a post when a reply would be useful to the author and visible to people
who care about running agent workflows: a question we can answer, a problem we
have a real opinion on, or a workflow someone wants to automate. Prefer authors
with more followers and posts with replies already forming, but a sharp
question from a small account beats a vague take from a large one.

Drop posts that are promotional, political, hostile, or about a controversy
where a reply means taking a side. Drop anything from `@outcomeci` or
`@ike4est`. Drop posts where the only honest reply is "use our product".

Write each `draft_reply` as the company: at most 240 characters, specific and
helpful, sounding like a team that runs agent workflows every day. No hashtags,
no em dashes, no exclamation marks. Mention the product only when it directly
answers the post. Say in `why` what the reply adds and who would see it.

Number `priority` from 1 for the best candidate. Return the candidates in
priority order, and a one-paragraph `summary` of what the scan found and which
topics drew the strongest conversations today. When `scan.already_replied` is
above zero, end the summary with "Skipped <N> posts @outcomeci already
replied to."

For each candidate also return its `id`: the numeric status ID from its URL,
copied exactly. Never invent a target, and do not repeat a target across
candidates.
