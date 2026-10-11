# ADR 0023: Webhooks tell API clients when their posts change state

Status: accepted

## Context

An API client (software or an AI agent) had to poll `GET /v1/posts/{post_key}` to learn that its post was approved,
published or failed. The owner asked for the server to call the client instead.

## Decision

- **Events** are state changes of posts a key created (`posts.source = 'agent'`): `post.approved`, `post.published`,
  `post.failed`, `post.expired`, `post.needs_check` (outcome uncertain: do not resend), `post.cancelled` (includes a
  rejection in the Sheet), `post.invalid`; plus `ping` for a test. Only moves INTO those states are reported.
- **Endpoints belong to a key** (max 3, `draft` right to create). The client registers, lists, tests (`ping`), reads
  its delivery log and deletes through `/v1/webhooks`; the owner lists and disables with `dk api webhooks` /
  `dk api webhook-disable`. A revoked key's endpoints stop receiving. `events` may narrow what is sent.
- **Never on the publishing path.** Messages are derived from `variant_events` *after* they are committed and sent by a
  separate Dagster run (`send_webhooks`, started by the `webhook_delivery` sensor only when something is due, one at a
  time through the `dk/webhooks` tag). A dead receiver can delay nothing but its own messages.
- **At-least-once, with a stable id.** `event_id = evt_<variant>_<seq>` is the same for every retry; a unique key
  `(endpoint, variant, seq)` makes queuing idempotent. A message is leased for 5 minutes when claimed, so two workers
  never hold it at once and a crashed one releases it. Retries after 1 min, 5 min, 30 min, 2 h, 6 h, 24 h; the 7th
  failure gives up (`dead`) and the owner is told on Telegram once (host only, never the path). HTTP 410 disables the
  endpoint. Redirects are not followed.
- **Signed.** `X-DK-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">` with the endpoint's `whsec_` secret,
  shown once at creation. Receivers refuse a timestamp older than 5 minutes (`domain/webhooks.py::verify`).
- **Only public https hosts.** Checked when registering and again when sending, and the connection goes to the
  address that was just checked (Host header and TLS name stay the client's, `sni_hostname`), so DNS rebinding between
  the check and the call cannot reach a private network. No error message or alert carries the address or the body.
- The webhook secret is stored in the database (it must be known to sign). Trade-off: a leak of that table lets someone
  forge messages to that one client; API keys stay hashed and the platform tokens are not in this database.

## Consequences

- Ordering is by event time per endpoint but not guaranteed (a retry can arrive after a later event): clients use
  `created_at` and `seq`-free idempotency on `id`, and re-read `GET /v1/posts/{post_key}` if unsure.
- Adding an event means: `domain/webhooks.py`, guide 02, `llms.txt`, and the tests that list them (see CHANGE-MAP).
