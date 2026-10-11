# Handoff: continue work on dk-publishing

Written 2026-10-11 (end of the second session) for the next Claude Code session. Read this first, then `CLAUDE.md`
(project rules, read automatically), **`docs/CHANGE-MAP.md`** (what to touch for each kind of change),
`docs/architecture.md` and `docs/adr/`. Nothing here contains a secret. Never ask the owner to paste tokens or keys
into chat; tell them to edit files or use their own terminal.

Start the new session in: `/Users/shadman.arko/Documents/Work_DoNotTouch/Dhaka Kacchi/dk-publishing`
(git branch `main`, last commits: webhooks (deployed, migration 0010 applied) and the JSON-label fix for `/v1/webhooks`; this file is tracked since commit `98462c0`; keep it updated).

## The project in one paragraph
`dk-publishing` is a business-agnostic social publishing pipeline for Dhaka Kacchi (a food business, Berlin):
Google Sheet + Drive folder (input) and an HTTP API (for other software) -> Postgres -> Dagster -> platform adapters.
It runs on the owner's Contabo VPS (`13.140.191.102`, shared with their live ordering system). It never posts twice
(compare-and-set + intent log + reconcile). All secrets live in ONE local file `~/.config/dk-publishing/dk.json`
(+ `google-key.json`); only `DATABASE_URL` and `DK_CONFIG_FILE` are environment variables.

## Where things stand (2026-10-11, 16:30 Berlin)
| Area | State |
| --- | --- |
| Facebook, Instagram (feed/carousel/reel/story), Threads, YouTube | `live`, verified with real posts |
| TikTok | `assisted`: a Telegram card + video is sent to the owner at the slot; they post by hand. Direct-post app is in TikTok review (low odds) |
| LinkedIn / X / Reddit | not built. Owner has **not answered** which to build. My recommendation: LinkedIn personal profile first (self-serve, no approval); X is pay-per-use; Reddit needs approval |
| 10-day posting plan | **Healthy.** At 16:16: 44 published, 28 waiting, last slot **15 Oct 16:30**, no failed/unknown/expired, TikTok cards sent every slot. **The plan ends 15 Oct: nothing is scheduled after it** |
| HTTP API (ADR 0020), `https://publish.dhakakacchi.com` | live, verified end to end by the owner with a test key (revoked) |
| Calendar approval (ADR 0022) | live and verified: agent posts wait as `awaiting_approval` in the Calendar tab; `approve`/`reject` tick boxes (far right) approve or cancel them |
| Media retention + upload limits (ADR 0021) | live: nightly `media_cleanup` 04:15 Berlin, 7-day grace; uploads refused at 85% disk (507), 5 GB per key (413), 512 MB per file; `media_expired` for removed files. First real cleanup run is the night of 11->12 Oct: check the Dagster run succeeded |
| Dagster run monitoring | on: a run killed by a deploy is failed after ~3 min instead of blocking the one-sync-at-a-time queue |
| Guides | `docs/guides/01-post-with-the-sheet.md` (humans) and `02-api-for-developers-and-agents.md` (developers/agents), PDFs in `docs/guides/pdf/` (`make guides`) |
| Docs stay true | `docs/CHANGE-MAP.md` + `tests/unit/test_docs_in_sync.py` (fails `make check` when guides, `llms.txt`, Sheet text or PDFs drift from the code) |
| Server disk | 42% (was 66%): ran `docker builder prune` (freed ~22 GB); `deploy.sh` now prunes cache older than 24 h each deploy. ~26 GB of old unused Docker images remain: do NOT prune without asking (may be the ordering system's) |
| Webhooks (ADR 0023) | **live and verified end to end** (2026-10-11: registered a receiver, `ping` 200, a rejected draft produced a signed `post.cancelled` delivered on attempt 1; test key and webhook removed). `/v1/webhooks` (register/list/delete/deliveries/ping), signed at-least-once delivery from a separate Dagster run (`send_webhooks`, sensor `webhook_delivery`), `dk api webhooks` / `dk api webhook-disable`. Guide 02 section 5b documents it |
| Tests | 1157 passed, 97% coverage |

## What the owner can do now (so you can answer questions)
- **Post from the Sheet:** Drive file -> Posts row -> one row per platform tab -> tick `ready` (guide 01).
- **Software posts:** key (`dk api key-create NAME --right draft`, owner's terminal only) -> upload -> validate -> create
  draft -> owner approves in the Calendar tab (guide 02). Platform-specific fields go inside `options`.
- **Calendar tab** shows every post (human and agent) sorted by date with `source`, `by`, `note`.

## Open items (in my suggested order)
1. **Next content batch.** The plan ends 15 Oct. Ask for the posts file or themes and dates; import with
   `scripts/import_posting_plan.py` (see how the first batch was done; the Sheets write quota is 60/min and the importer paces it).
2. **Owner to-dos:** untick `ready` on the stray `DK-TT-DEMO` Sheet row (1 Dec, TikTok, currently `draft`); switch the TikTok
   account back to public; delete test posts on platforms; old `invalid` rows `DK-TEST-1/-3/-YT-1` are harmless leftovers.
3. **Which new platform?** LinkedIn / X / Reddit: wait for the answer; do not start building without it.
4. **Performance report** (views/likes/reach by platform, format, time). Offered, unanswered; do it after a few more days of data.
5. Not built: editing a post in place (cancel and resubmit today), per-key rate limiting (reverse proxy), MCP wrapper for agents,
   token vault, TikTok/LinkedIn/X/Reddit adapters. Known limits: DNS-rebinding window in the URL fetcher (ADR 0020).
6. Verify the TikTok hand-over card fix (videos up to 49 MiB attached, else a 24 h download link) on a slot.
7. Optional: let the owner decide about `docker image prune` (26 GB) if disk climbs again.

## Decisions on record (do not re-litigate)
- **Approval stays in two places:** the owner's own Sheet posts via `ready` (Posts tab); agent posts via Calendar ticks. The owner
  declined a Calendar-only approval for their own posts. Revisit only if someone else starts typing rows.
- API posts never appear in the Posts/platform tabs (the sync never edits or cancels them); the Calendar is the single view of everything.
- Per-platform filter views on the Calendar were chosen over mirrored read-only per-platform tabs (not yet built; offered).

## Deploy and operate
- Server details are in the git-ignored `deploy/server.conf`. `make check` before every commit (see the pitfall below).
  `make deploy` ships code (and runs the config check and the build-cache prune); `make deploy-secrets` ships dk.json.
  `make sheet-init` after any change to `config/sheet.yaml` (adds headers, re-applies formatting and checkboxes, never clears data).
  `make guides` after editing a guide (needs Chrome).
- Dagster UI: `ssh -L 3000:localhost:3000 root@13.140.191.102`, then http://localhost:3000.
- Server stack folder `/srv/dk/app/deploy`: `docker compose -f compose.prod.yaml --env-file .env ...`; services postgres, migrate,
  daemon, webserver, api, media (Caddy), backup. Compose project name must stay `dk-publishing`.
- Read-only health check (what I ran today): posts by status and what is due via
  `... exec -T postgres psql -U postgres -d dk_publishing -c "select ..."` on `publishing.variants` / `publishing.posts` /
  `publishing.variant_events`; Dagster failures via `DagsterInstance.get().get_run_records(RunsFilter(statuses=[...]))` inside the
  `webserver` container; alerts in `publishing.alerts_sent`; disk with `df -h /` and `docker system df`.
- Stuck Dagster run after a deploy (should now self-heal): `DagsterInstance.get().report_run_failed(run)` in the webserver container.
- Preview the media cleanup: `dk media purge --dry-run` (in the `api` container).
- The live ordering system's Caddy (`/opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/Caddyfile`) owns 80/443 and proxies `media.` and
  `publish.dhakakacchi.com` to `dk-media:80`. Always back it up, `caddy validate`, then reload.

## Rules learned the hard way (also in CLAUDE.md)
- Migrations are forward-only: never edit an applied one (`0010_webhooks.sql` is the latest, applied on the server). Every unique key must include `tenant_id` (a test enforces it).
- Never run `down -v` or anything that resets a database holding live tokens/publish state.
- Do NOT add `from __future__ import annotations` to Dagster `jobs.py`/`sensors.py` or the API `app.py`.
- Platform names appear only in `adapters/platforms/`, `config/` (a test enforces this).
- A call that may have reached a platform is `UnknownOutcome`, never `Retryable`.
- Sheets API: write only changed cells, `valueInputOption=RAW`; 60 writes/min quota; an unticked checkbox reads back as `FALSE`.
- **Never `values.clear` a checkbox column** (it removes the boxes): the Calendar rewrite clears system columns one by one and writes FALSE into `approve`/`reject`.
- **Do not chain `make check | tail && git commit`**: the pipe hides a failing check (I committed red once and had to amend). Check the exit code.
- Platform-specific API fields live under `options`; a test proves every example the API hands out is accepted.
- `dk.json` was once wiped by a stale editor window: tell the owner to close the editor after saving.
- Deploys can catch a Dagster run mid-start; monitoring now fails it, but glance at open runs after a deploy.

## Working with the owner
- They dictate by voice (messages are long and rambling). Give short, concrete, numbered steps; show exact commands in their own
  code block; say which machine a command runs on (their Mac vs the server over SSH).
- Their terminal turns a typed `@` in pasted commands into a full-width `＠` sometimes: prefer `curl -T file` over `--data-binary @file`.
  The shell is zsh (use `read -rs "KEY?Prompt: "`, not bash's `read -s -p`). They never paste keys in chat; keys are created in their terminal.
- Ask before big work ("human in the loop"); they say "yes, run it" / "deploy it" for production actions, and that approval is per action.
- Never raise the German legal-notice (Impressum) topic.
- The auto-mode safety classifier may block production actions or risky deletes. When denied, do not work around it: explain, and let
  the owner run it or allow it.
