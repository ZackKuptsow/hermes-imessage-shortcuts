# HTTP API

The plugin's listener, on the loopback address. Gate 3 below is the third gate in
[What it refuses](security.md#what-it-refuses).

## Why a request was dropped

Every `204` says why it dropped a request, in an `X-Imessage-Dropped` header holding exactly
one of `sender`, `empty` or `duplicate`, and no other answer carries it. On `POST /imessage`
those are a sender off the allow list, an empty text, and a repeat of a text already taken.
A caller such as an n8n relay can then alarm on a sender drop and let a safe retry pass.

## Sending a text without the model

`POST /imessage/send` sends one text without the model, so a loopback caller holding the
secret (n8n's alarms) can reach your phone through the gateway's existing Automation grant.
Its body is a JSON object of two strings, `to` and `text`. It passes the same three checks
first (`403` off the loopback address, `503` while the platform is not serving, `401`
without the secret), then `400` for a malformed body. The text leaves the way a reply does,
through gate 3: a recipient off the allow list is a `204` with `X-Imessage-Dropped: sender`,
and an empty text is a `400`, since the caller composed it and a `204` would read as
success. So is a text holding a NUL or a lone surrogate, which imsg cannot be handed or
Messages cannot show: it is refused whole, before any of a long text goes out, and never
sent altered. imsg never runs for any of these. `202` means imsg reported `sent`. `502` means
it did not, with imsg's word for why in a JSON body,
`{"disposition": "not_started"}`, and never the text. A text over 4,000 characters goes out
as several chunks, and when one after the first fails the body also says how far it got,
`{"delivered_chunks": 1, "disposition": "not_started", "total_chunks": 2}`.
`delivered_chunks` counts the chunks imsg reported `sent` before the one that failed, whose
own disposition still applies to it (with `may_have_completed` it may have arrived too). A
`502` carrying the counts is not safe to retry whole, whatever its disposition: a retry
would resend the chunks already sent. The endpoint opens no session and checks no
toolsets: no text reaches it, and the model has no tool that calls it. So while a widened
profile has every text answered `503`, the alarm about it still goes out, until the
platform next connects. A reconnect or a restart reruns the start checks, which refuse the
widened profile; from then on `/imessage/send` answers `503` too, or nothing listens, and
n8n's run record and its log are what report it.
