# The HTTP API

> New here? The step-by-step guides are in [`docs/guides/`](../guides/README.md): [posting with the Sheet](../guides/01-post-with-the-sheet.md) and [the API for developers and agents](../guides/02-api-for-developers-and-agents.md). This page is the short reference.

Another program (a content tool, an AI agent) can schedule posts here instead of through the Google
Sheet. It talks to the same engine: validation, approval, scheduling, retries and "never post twice"
are shared, so a post made by the API behaves exactly like one made from the Sheet.

**Plain-text version for agents:** `GET /llms.txt`. **Machine-readable contract:** `GET /openapi.json`
(interactive page: `/docs`). Both are served by the API itself and need no key.

## The four calls

| Step | Call | What it does |
| --- | --- | --- |
| 1. Learn | `GET /v1/platforms` | What every platform accepts: formats, caption limits, number and type of files, extra fields, connected accounts. |
| 2. Upload | `POST /v1/media?filename=reel.mp4` | Send a file once, get an id. (Or send `{"url": "https://..."}` and the server downloads it.) |
| 3. Check | `POST /v1/posts/validate` | Say exactly what is wrong, or `{"valid": true}`. Creates nothing. |
| 4. Create | `POST /v1/posts` | Creates the post as a **draft**; a person approves it. |

Then `GET /v1/posts/{post_key}` shows where each platform stands.

## A first run (copy and paste)

```bash
API=https://publish.yourbusiness.com
KEY=dkp_...                      # issued by the owner, see "Keys" below

# 1. what does Instagram accept?
curl -s -H "Authorization: Bearer $KEY" $API/v1/platforms/instagram

# 2. upload the video once
curl -s -H "Authorization: Bearer $KEY" -X POST --data-binary @reel.mp4 "$API/v1/media?filename=reel.mp4"
#    -> {"media_id": "3f2a...", ...}

# 3. the post: one request, several platforms
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -X POST $API/v1/posts -d '{
  "post_key": "eid-reel-1",
  "variants": [
    {"platform": "instagram", "format": "reel", "caption": "Slow-cooked for Eid.",
     "media": ["3f2a..."], "slot": "2026-10-12T19:30:00+02:00"},
    {"platform": "youtube", "format": "video", "media": ["3f2a..."],
     "slot": "2026-10-12T19:30:00+02:00",
     "options": {"title": "Dum biryani in 40 seconds", "made_for_kids": "no"}}
  ]
}'
```

Send `Content-Type: application/json` when you can. The post endpoints also read a JSON body that arrives
with no label or with curl's default form label.

## What a refusal looks like (this is the point)

Mistakes are answered with every problem at once, where it is, and what is allowed, plus a valid example:

```json
{
  "status": 422,
  "title": "The request has problems",
  "problems": [
    {"path": "variants[0].caption", "code": "too_long", "limit": 2200, "actual": 2410,
     "message": "Caption is 2,410 characters; Instagram allows 2,200."},
    {"path": "variants[1].options.privacy_level", "code": "not_allowed",
     "allowed": ["public", "friends", "followers", "only_me"]}
  ],
  "examples": {"variants[0]": {"platform": "instagram", "format": "reel", "caption": "…", "slot": "…"}}
}
```

A program fixes `path` and sends again; an agent needs no manual. The numbers come from the platform
adapters themselves (a test checks every guide example against the real validation), so they cannot go stale.

## Safe by construction

- **Repeating is safe.** `post_key` is your own id for the post. Send it again with the same content (after a
  timeout) and you get the existing post back (`200`, `Idempotent-Replay: true`), never a second one. The same
  `post_key` with different content is `409`: cancel and use a new key.
- **Drafts wait for you.** A normal key creates drafts; the owner is told on Telegram and approves by ticking
  `approve` on the Sheet's Calendar tab (or with `dk api approve <post_key>`). `reject` cancels the draft. A key with the `approve` right may publish directly (`"approve": true`).
- **Typos are caught.** Unknown fields and unknown options are refused, not ignored.
- **Times need an offset.** `2026-10-12T19:30:00+02:00` or `...Z`. A bare time is ambiguous and refused.

## Files on the server

Uploads are kept only while a post needs them: a week after a post is finished (published, cancelled, failed) its
file is removed, and a post naming it gets `media_expired` (upload again). Limits: 512 MB per file, 5 GB per key
kept at once (`413 quota_exceeded`), and uploads pause with `507 storage_full` when the server disk is 85% full.
See ADR 0021.

## Keys (owner)

```bash
dk api key-create content-agent --right draft     # prints the key ONCE; give it to that program
dk api keys                                       # who has a key, when last used
dk api key-revoke content-agent                   # stops it immediately
```

Rights: `read` (look only) < `draft` (create and cancel drafts, upload) < `approve` (publish directly).
Only a hash of the key is stored. On the server, run these with
`docker compose -f compose.prod.yaml --env-file .env exec api dk api key-create ...` from `/srv/dk/app/deploy`.

## Turning it on

1. Point a domain at the server (for example `publish.yourbusiness.com`), as you did for the media domain.
2. Put `API_DOMAIN=publish.yourbusiness.com` in `deploy/server.conf`, then `make deploy`.
3. On a shared server add one block to the existing web server's Caddyfile (as for the media domain), then reload it:
   ```
   publish.yourbusiness.com {
   	reverse_proxy dk-media:80
   }
   ```
4. `curl https://publish.yourbusiness.com/healthz` answers `{"status":"ok"}`.

## Not in this version

Webhooks (tell the caller when a post is published), editing a post in place (cancel and resubmit), and an MCP
wrapper for AI agents. Today a caller polls `GET /v1/posts/{post_key}`.
