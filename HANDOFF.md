# Handoff: continue work on dk-publishing

Written 2026-10-11 for a new Claude Code session (another account). Read this first, then `CLAUDE.md`
(project rules, read automatically), `docs/architecture.md` and `docs/adr/`. Nothing here contains a
secret. Never ask the owner to paste tokens or keys into chat; tell them to edit files themselves.

Start the new session in: `/Users/shadman.arko/Documents/Work_DoNotTouch/Dhaka Kacchi/dk-publishing`
(git branch `main`, last commit `360681c`; this file is untracked on purpose).

## The project in one paragraph
`dk-publishing` is a business-agnostic social publishing pipeline for Dhaka Kacchi (a food business, Berlin):
Google Sheet + Drive folder (input) -> Postgres -> Dagster -> platform adapters. It runs on the owner's Contabo
VPS (`13.140.191.102`, shared with their live ordering system). It never posts twice (compare-and-set + intent
log + reconcile). All secrets live in ONE local file `~/.config/dk-publishing/dk.json` (+ `google-key.json`);
only `DATABASE_URL` and `DK_CONFIG_FILE` are environment variables.

## Where things stand (2026-10-11)
| Area | State |
| --- | --- |
| Facebook, Instagram (feed/carousel/reel/story), Threads, YouTube | `live`, verified with real posts |
| TikTok | `assisted`: a Telegram card + video is sent to the owner at the slot; they post by hand. Direct-post app was submitted to TikTok review (low odds of approval; owner waits) |
| LinkedIn / X / Reddit | not built. Last open question to the owner: build X, LinkedIn (personal or company page), or both? Reddit needs approval, X is pay-per-use, LinkedIn personal profile is self-serve. Do not start building until they answer |
| 10-day posting plan | 65 posts approved on the server through 15 Oct, slots 11:00 / 16:30 / 19:30 Berlin. Check Telegram/Dagster for failures and fix/re-run any failed post |
| Telegram alerts, daily digest 08:00, nightly token renewal 03:30, nightly login check 03:45 | running |
| HTTP API (ADR 0020, commit `360681c`) | **built, tested (1069 tests, 97% coverage), deployed, and live at `https://publish.dhakakacchi.com`**. The owner created a key and confirmed `GET /v1/platforms` works. Caddy block for the domain was added by the owner by hand on the server (backup `Caddyfile.bak` next to the file) |

## The HTTP API (what was just built)
Another program or an AI agent can schedule posts without the Sheet. Read `docs/api/README.md` (human) and
`src/dk_publishing/entrypoints/publish_api/llms.txt` (served at `/llms.txt`). Flow: `GET /v1/platforms` (rules per
platform) -> `POST /v1/media` (upload, get an id) -> `POST /v1/posts/validate` -> `POST /v1/posts` (creates DRAFTS;
the owner approves with `dk api approve <post_key>` or an `approve`-right key submits with `"approve": true`).
Errors are RFC 9457 problems listing every issue with path, code, limit/actual, allowed values, and an example.
`post_key` is the idempotency key. Keys: `dk api key-create NAME --right read|draft|approve`, `keys`, `key-revoke`.
Key design points are in `CLAUDE.md` (last bullet) and `docs/adr/0020-http-api-for-other-software.md`.

### Not done yet in the API (Phase 2 / 3, owner has not chosen)
- Safe end-to-end test through the public address (upload a small file, a deliberate mistake to show the teaching
  error, a draft, then cancel it). The owner runs curl with their own key so the key never enters chat.
- Confirm `tiktok` appears in `GET /v1/platforms` (it is `assisted`, so it should; I never verified the full list).
- Phase 2: webhooks (tell the caller when a post is published), editing a post in place, rate limiting (reverse proxy).
- Phase 3: an MCP wrapper so AI agents get `list_platforms` / `validate_post` / `submit_post` as tools; a small client.
- Known limits: DNS-rebinding window in the URL fetcher (documented in ADR 0020); in `dry_run` mode adapters validate
  less than live ones.

## Other open items (owner side)
- TikTok app review pending; untick `ready` on the stray `DK-TT-DEMO` (1 Dec) Sheet row; switch the TikTok account
  back to public; delete test posts on platforms.
- Offered, unanswered: a performance report after a few days (views/likes/reach by platform, format and time).
- Verify the TikTok hand-over card fix (videos up to 49 MiB attached, else a 24h download link) on the next slot.

## Deploy and operate
- Server details are in the git-ignored `deploy/server.conf` (SERVER, DOMAIN=media.dhakakacchi.com,
  SHARED_NETWORK=deploy_dhaka-kacchi, ACCOUNT_NAME, API_DOMAIN=publish.dhakakacchi.com).
- `make check` (ruff, mypy --strict, import-linter, tests with >=90% coverage) before every commit.
  `make deploy` ships code; `make deploy-secrets` ships dk.json (see `docs/setup/update-secrets.md`).
- Dagster UI: `ssh -L 3000:localhost:3000 root@13.140.191.102`, then http://localhost:3000.
- Server stack folder: `/srv/dk/app/deploy` (`docker compose -f compose.prod.yaml --env-file .env ...`); services:
  postgres, migrate, daemon, webserver, api, media (Caddy), backup. Compose project name must stay `dk-publishing`.
- The live ordering system's Caddy (`/opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/Caddyfile`) owns 80/443 and
  proxies `media.` and `publish.dhakakacchi.com` to `dk-media:80`. Always back it up, `caddy validate`, then reload.

## Rules learned the hard way (also in CLAUDE.md)
- Migrations are forward-only: never edit an applied one (`0008_api.sql` is applied on the server).
- Never run `down -v` or anything that resets a database holding live tokens/publish state.
- Do NOT add `from __future__ import annotations` to Dagster `jobs.py`/`sensors.py` or the API `app.py`.
- Platform names appear only in `adapters/platforms/`, `config/` (a test enforces this).
- A call that may have reached a platform is `UnknownOutcome`, never `Retryable`.
- Sheets API: write only changed cells, `valueInputOption=RAW`; 60 writes/min quota.
- `dk.json` was once wiped by a stale editor window: tell the owner to close the editor after saving.

## Working with the owner
- They dictate by voice (messages are long and rambling). Give short, concrete, numbered steps; show exact commands
  in their own code block; tell them which machine a command runs on (their Mac vs the server over SSH).
- They want a question first before big work ("human in the loop"); they have allowed approving Sheet rows.
- Never raise the German legal-notice (Impressum) topic.
- The auto-mode safety classifier may block production actions (`make deploy`, edits to the server's Caddyfile over
  SSH, external tunnels). When denied, do not work around it: explain, and let the owner run it or allow it.
- Never reveal or ask for secrets in chat; API keys are created by the owner in their own terminal.
