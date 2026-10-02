# Working context — dk-publishing

`docs/architecture.md` is the map; `docs/adr/` records why. This file lists what will bite you.

- **Dependency rule:** `domain` imports only the stdlib; `application` only `domain`; only `composition.py`
  constructs adapters; nothing imports `entrypoints`. Enforced by `make arch` and `tests/unit/test_architecture.py`.
- **Platform names** (instagram, facebook, ...) appear only in `adapters/platforms/` and `config/`. The scheduler
  reads `Capabilities`, never branches on a platform.
- **Never post twice.** No blind retry after a sent publish request. Uncertain outcome -> `unknown` -> reconcile.
- **Only `adapters/secrets` imports `cryptography`.** No LLM SDK is ever imported here.
- **Times:** UTC `timestamptz` in storage; Europe/Berlin only in the Sheet and messages. Reject DST-ambiguous times.
- **Separate from the harness repo** on purpose (ADR 0013). Never run anything that downgrades/reset this schema
  against a database holding live tokens or publish state.
- Run `make check` before every commit.
- **Migrations are forward-only** (ADR 0015). Never edit an applied `migrations/*.sql`; add a new file. There is no
  downgrade. `variant_events` is append-only (trigger). Call `variants.apply()` for every state change: it is the
  compare-and-set that prevents double publishing, and it returns False when you lost the race, so check it.
- **Integration tests need Postgres:** set `TEST_DATABASE_URL`, or have PostgreSQL binaries installed (a throwaway
  cluster is started automatically). Under `CI=true` a missing database fails the build instead of skipping.
- `sheet_snapshots.cells` is named that because `values` is a reserved word (same trap as the harness's `orders`).
- **Publish ordering is the duplicate guarantee** (`application/use_cases/publish.py`): CAS into `publishing` + intent
  log, commit, *then* the platform call with no transaction open, then record the outcome. Never move the call inside a
  transaction or before the commit. A run that dies leaves `publishing`; housekeeping -> `unknown` -> reconcile.
- **Adapters raise the five domain errors.** A call that may have reached the platform raises `UnknownOutcome`, never
  `Retryable`. Unexpected exceptions are treated as `UnknownOutcome` by the use cases. `find_live` returns None only
  for "confirmed not live"; if the platform cannot answer it must raise.
- **New adapter = subclass `tests/contract/publisher_contract.py::PublisherContract`.** Test doubles are in
  `tests/support` (`ScriptedPublisher` injects failures, `SimulatedCrash` is a BaseException that kills a run).
- **Dry-run mode** (`adapters/platforms/dry_run.py`) writes to `publishing.dry_run_posts`. It has no unique key on
  variant on purpose: a duplicate must show up as two rows.
- Use cases take `(services, variant_id, expected_version)`; a version mismatch returns `SKIPPED`, a lost
  compare-and-set returns `LOST_RACE`. Both are normal. Use-case tests run against real Postgres, not in-memory fakes.
- **Dagster is a thin shell** (`entrypoints/dagster_defs/`): ops are build/call/log, the `due_actions` sensor turns
  `variants.due()` into runs keyed `<action>:<variant_id>:<version>`. Do NOT add `from __future__ import annotations` to
  `jobs.py`/`sensors.py`: Dagster resolves op/sensor parameter annotations at runtime and breaks on strings.
  `DAGSTER_HOME` must be an absolute path (the Makefile sets it). Runs are tagged `dk/account`; `dagster/*.yaml` allows
  one run per account at a time.
- **platforms.yaml:** every non-`off` platform is `dry_run` until a real adapter exists (the loader refuses
  `assisted`/`live`). YAML reads an unquoted `off` as `False`; the loader accepts it, don't "fix" that.
- **Local loop:** `make db-up && make migrate && make dagster-dev`, then `make seed-rehearsal` and watch
  localhost:3000. `.env` (git-ignored) holds identifiers only (Sheet/Drive/Meta IDs); keys go outside the repo.
- **Sheet sync** (`application/use_cases/sync_sheet.py`, rules in `domain/sync.py`): rows match variants by
  `(post_key, platform, account)`. `variants.source_hash` is the hash of everything editable that affects a variant
  (row + Posts row + Drive checksums, never the title); a change after approval withdraws it and re-approves if
  `ready` is still ticked. Cancelling stores the row's hash (or `removed`) so putting the row back revives the post.
  Slot-in-the-past is only checked when creating/re-approving, never for a post already waiting.
- **Sheets API traps (found against the real API, not the fakes):** an unticked checkbox comes back as `FALSE` in every
  row, so "row is empty" must ignore `False` and the "last used row" must too. Write with `valueInputOption=RAW`
  (an error message starting with `=` must not become a formula). Write only changed cells, or the write-back's own
  edit re-triggers the sensor forever; a no-op sync must write 0 cells.
- The `sheet_changed` sensor costs one extra no-op sync after our own write-back (Drive's modifiedTime moves).
  `sheet_sync_fallback` (15 min) exists because a failed sync would not otherwise retry until the Sheet changes.
- More than 5 scheduled cancellations in one sync halts it (nothing applied, the Dagster run fails). Override with
  `dk sync --allow-cancellations`. The `failed_run_alert` sensor now sends that failure to Telegram.
- `dk sheet sample` / `dk account add` create dry-run accounts; real ones come from the (unbuilt) connect flow.
- **Real platform adapters** (`adapters/platforms/meta.py`, `facebook.py`): `GraphClient` maps every Meta answer to the
  five domain errors. A write whose answer is lost, or a 5xx on a write, is `UnknownOutcome` (never `Retryable`);
  only reads and "could not connect" are safely retryable. Tokens ride in the POST body / GET query (Meta's documented
  way) and are redacted from every message. Never log a URL or request body. See ADR 0016 and
  `docs/runbooks/facebook-live-test.md`. `mode: live` is a deliberate switch in `config/platforms.yaml`: the shipped
  config is dry_run everywhere, and a test asserts it, so nothing posts by accident.
- Media for live platforms is downloaded from Drive during `prepare` (`DriveMediaStore`, named by checksum, verified
  against the approved md5). No transcoding yet. A checksum mismatch is `Retryable`: the next sync will notice the edit.
- Not built: the token vault, TikTok/LinkedIn/X/Reddit adapters.
- **Delivery** (ADR 0017): a row's `delivery` is `direct` (we publish at the slot) or `native` (the platform holds the
  post). `planning_caps()` hides a platform's native window unless the row says `native`; every `plan_next(APPROVED)` must
  use it. Rules that must not regress: withdraw at the platform BEFORE changing a natively scheduled variant, never cancel
  at or after the slot, and never retry an uncertain schedule (it FAILS for a person: a hidden copy may exist).
- **Telegram alerts** (ADR 0018): `application/use_cases/alerts.py`. A message is recorded in `publishing.alerts_sent`
  only AFTER Telegram accepted it, so an outage delays alerts and never loses or repeats them. Alerts come from
  `variant_events` (failed/expired/unknown), looking back 24h only. The `notifications` sensor sends them and pings
  `HEARTBEAT_URL` inside its tick (no job per minute); `failed_run_alert` is deduped per job per hour; `daily_digest`
  runs 08:00 Berlin, once per day. No `TELEGRAM_CREDENTIALS_FILE` = alerts silently off. The bot token is part of every
  request URL: never let an httpx exception message out (`telegram._call` swallows it on purpose).
- **One configuration file** (`dk.json`, `adapters/platforms/dk_file.py`): every id, key and token. Only `DATABASE_URL`
  and `DK_CONFIG_FILE` are environment. `composition.environment()` = os.environ + dk.json translated to the older env
  names (explicit env still wins), and both `cli.py` and the Dagster resources must use it, never `os.environ`.
  Credentials classes take a reference `path#section` (`adapters/config/secrets_file.py`) so a renewed token writes back
  into its section only. Setup guides are in `docs/setup/`; `make check-setup` and `dk live-test <platform> --yes`
  (really posts, reads it back) are the proof commands. New platform = a dk.json section, a guide, a check in
  `setup_check.py`, and a `live_test.py` branch.
- **YouTube's uploads list lags a fresh upload** (found in the first real test: read-back immediately after upload saw
  nothing). `find_live` therefore re-checks 4 times, 15 s apart, before saying "not live"; a hit returns at once. Do not
  shorten this: "not live" makes reconcile upload again. Tests pass `sleep=lambda _: None`.
- **Instagram reel upload (`rupload`) returned HTTP 500 `ProcessingFailedError` for this app** on every file and header
  variant (2026-10-02, valid token and scopes); container creation worked. Planned workaround: `video_url` from the public
  media link once the server has one.
- **Production stack** (`Dockerfile`, `deploy/`): one image for migrate/daemon/webserver; Caddy serves only
  token-shaped paths from the `public` volume (and mounts `media` read-only because the links are symlinks into it);
  the Dagster UI listens on 127.0.0.1 only (SSH tunnel). `deploy/platforms.production.yaml` is mounted over
  `config/platforms.yaml`, and the shipped config stays dry-run. Secrets dir `/srv/dk/secrets` is owned by uid 10001
  and must stay writable (tokens renew in place). Only ONE compose service may carry `build:` (migrate), or parallel
  builds collide on the image name. Nightly `renew_tokens` and the 5-minute public-link sweep are what keep it unattended.
- **Shared servers:** the Contabo server already runs the ordering system (project `deploy`, its Caddy owns 80/443 and
  serves `api.dhakakacchi.com`, network `deploy_dhaka-kacchi`). Deploy there with `--shared deploy_dhaka-kacchi` and add
  a `media.dhakakacchi.com { reverse_proxy dk-media:80 }` block to that Caddyfile (`/opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/Caddyfile`).
  Our compose project is named `dk-publishing` on purpose; never run it from a folder-derived name there.
- **When the user reports an expired/broken token or login:** the fix is documented in `docs/setup/update-secrets.md`
  (edit local `dk.json`, `make check-setup`, `make deploy-secrets`). Server details are in the git-ignored
  `deploy/server.conf` (copy of `deploy/server.conf.example`). Walk them through that page; never ask for token values in chat.
