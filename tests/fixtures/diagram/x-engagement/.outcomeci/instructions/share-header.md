# Post the thread header

Read digest.summary, digest.candidates, and trigger. Post exactly one top-level
message to growth using slack.post. Omit thread_ts entirely. The first line must
be `Daily X leads for @outcomeci on <date>: <N> posts worth a reply`, where N is
the candidate count. Use trigger.scheduled_at converted to America/Chicago for
the date when available. Follow the first line with a blank line and the digest
summary. Do not change candidate drafts, post candidate replies, or post any
other message in this step.

After the summary, add a blank line and: "Each draft is in this thread with a
*Reply on X* link that opens the reply pre-filled. Sign in as @outcomeci, edit
if you like, and post. Nothing is posted automatically." This is part of the
single header, not a second post.

Wait for the actual call result. If it is denied, fails, or does not return a real
Slack ts, stop immediately and return posted: false. Do not retry with modified
wording, invent a timestamp, or use PENDING. Only return posted: true after a
successful call. The next step reads the broker's recorded call result, not a
timestamp supplied in your answer. Denied attempts are not completed posts.

Use actual newline characters in Slack text, never literal backslash-n sequences.
