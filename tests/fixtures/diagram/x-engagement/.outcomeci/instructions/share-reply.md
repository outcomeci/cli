# Post one recommendation in the confirmed header thread

This invocation handles only `candidate`, one item from digest.candidates in
priority order. The header is already posted. Read its actual ts from
share_header.calls.slack.post. Use that exact ts as thread_ts, never a placeholder.
Post exactly one reply with slack.post using channel: "growth" exactly, as
required by the grant. Do not substitute the returned Slack channel ID.

Build the reply link as
`https://x.com/intent/post?in_reply_to=<id>&text=<encoded draft>` where `<id>`
is candidate.id and `<encoded draft>` is candidate.draft_reply percent-encoded
for a URL query value: encode every character except A-Z, a-z, 0-9, `-`, `_`,
`.` and `~` (a space becomes `%20`, a newline `%0A`, `&` becomes `%26`, `#`
becomes `%23`, `'` becomes `%27`, `|` becomes `%7C`, `>` becomes `%3E`, `<`
becomes `%3C`, and every non-ASCII character is its UTF-8 bytes, each as
`%XX`). Check that decoding it gives back the draft exactly.

Use this format, in Slack mrkdwn:

*<priority>.* <author>  ·  <url>

*Why:* <why>

*Draft:*
> <draft_reply>

<link|Reply on X →>

Preserve the supplied draft verbatim; if it contains newlines, prefix every
draft line with >. No extra posts, edits, reactions, hashtags or em dashes. Do
not post a new header.

Wait for the actual result. If denied or failed, stop immediately; do not retry or
claim success. Return posted: true only for a successful confirmed reply, otherwise
posted: false. Return priority as this candidate's priority. Denied and reviewing
receipts are not posted messages. Each for_each invocation has only one candidate;
it does not need to post or review the other candidates.

Use actual newline characters in Slack text, never literal backslash-n sequences.
