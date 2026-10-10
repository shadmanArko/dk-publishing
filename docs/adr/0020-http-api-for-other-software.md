# 0020. An HTTP API for other software, over the same engine

Status: accepted

## Context
Posts could only enter through the Google Sheet. Other software (a content generator, an AI agent) needs to
submit posts, learn each platform's rules without a person explaining them, and be told precisely what to
fix when it gets something wrong.

## Decision
- A FastAPI app in `entrypoints/publish_api`, a thin shell like the Dagster one: it authenticates and
  translates. Every rule lives in `application/use_cases/api_posts.py` and the platform adapters.
- **One source of truth for rules.** `adapters/platforms/guides.py` states the limits using the adapters'
  own constants; `tests/unit/test_guides.py` runs every example, limit, choice and media count through the real
  `validate`. The API serves the guides at `GET /v1/platforms`.
- **Errors teach.** RFC 9457 problem documents with a `problems` list: JSON path, stable code, limit/actual,
  allowed values, expected media, and a valid example. All problems are returned in one answer.
- **A submitted variant is an ordinary draft** and approval is the same `approve` use case as the Sheet's, so the
  scheduler, `plan_next`, native/direct delivery and the duplicate guarantees apply unchanged.
- **`post_key` is the idempotency key**, with a stored hash of the request: same key + same request returns the
  existing post, same key + different request is a conflict. A concurrent repeat is settled by the unique key.
- **Drafts by default.** Keys have rights read < draft < approve. Only `approve` may submit with `approve: true`.
  A new draft pings the owner on Telegram. Keys are random, stored as SHA-256 only.
- **The Sheet leaves API posts alone.** `posts.source = 'agent'` (the column already existed): the sync does not
  cancel, edit or adopt them, and a Sheet row that reuses their key is told why it is ignored.
- **Files are uploaded first and named by id** (`api_media`, kept as `<sha256>-<name>` under the media dir), either
  as bytes or fetched from a public https link (private/loopback addresses and redirects into them are refused).
  The media store routes by item: `media_id` to uploads, otherwise to Drive.
- Slots must carry a UTC offset: a bare time cannot be interpreted safely.

## Consequences
- Another system can integrate from `/openapi.json` and `/llms.txt` alone.
- Adding a platform means adding its guide; a missing guide hides the platform from the API rather than guessing.
- Not built: webhooks, editing in place, rate limiting (put it in the reverse proxy), an MCP wrapper.
- Residual risk: the link fetcher checks DNS before connecting, so a DNS-rebinding host could in theory answer
  differently the second time. Keys are issued only to trusted software, which bounds this.
