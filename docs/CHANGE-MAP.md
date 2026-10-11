# Change map: when you change X, also touch Y

Read this **before** adding or changing a feature. One change usually lives in several places (code, the Sheet,
the API help, two guides, their PDFs, tests, the live Sheet, the server). Missing one is how bugs and stale
documents happen. This page lists the places for each kind of change, in the order to do them.

Some of it is enforced: `tests/unit/test_docs_in_sync.py` fails `make check` when the guides, the agent text
(`llms.txt`), the Sheet instructions or the PDFs no longer match the code. It cannot catch everything: the lists
below are the rest.

## Always, for every change

1. **Find the row below** that matches your change and do every item in it.
2. **A decision or trade-off?** Add or update an ADR in `docs/adr/` and its row in `docs/adr/README.md`.
3. **Something that will bite the next person?** Add one bullet to `CLAUDE.md`.
4. **User-visible?** Update both guides if they are affected (`docs/guides/01-post-with-the-sheet.md`,
   `docs/guides/02-api-for-developers-and-agents.md`), then run **`make guides`** (needs Chrome) so the PDFs match.
5. **`make check`** must pass (lint, types, import rules, tests, 90% coverage). Never commit on red.
6. **Commit, then ship** with the "After merging" steps at the bottom (deploy, and `dk sheet init` if the Sheet changed).

## If you change...

### A platform's rules (a format, a limit, an option, a file type)
- The adapter: the platform's own module in `src/dk_publishing/adapters/platforms/` (`validate`, constants)
- What the API teaches: `src/dk_publishing/adapters/platforms/guides.py` (a test proves it against the real `validate`)
- The Sheet columns: `config/sheet.yaml` (`platform_extras`, dropdown options and notes)
- Guide 01 tables (files per platform, platform columns, mistakes) and guide 02 (examples, cheat sheet)
- Tests: the platform's file in `tests/unit/` (named after the platform), `tests/contract/` (the contract suite), `tests/unit/test_guides.py`
- `dk live-test` branch in `src/dk_publishing/adapters/platforms/live_test.py` if posting changed

### A new platform
Everything in the row above, plus:
- `config/platforms.yaml` (starts as `dry_run`), `src/dk_publishing/adapters/platforms/registry.py` and `live.py`
- `src/dk_publishing/composition.py` (wiring, account creation), a `dk.json` section in `src/dk_publishing/adapters/platforms/dk_file.py`
- `src/dk_publishing/adapters/platforms/setup_check.py` (a `check-setup` line) and a guide in `docs/setup/`
- A new tab in the Sheet (`config/sheet.yaml`, then `dk sheet init`), and `dk account sync`
- `src/dk_publishing/entrypoints/publish_api/llms.txt` (the list of platforms), guide 01 and 02, `README.md`
- A contract test subclass in `tests/contract/` (`PublisherContract`)
- Platform names may appear only in `adapters/platforms/` and `config/` (enforced)

### An API endpoint, field or error code
- `src/dk_publishing/entrypoints/publish_api/app.py` (routes, request bodies) and `src/dk_publishing/application/use_cases/api_posts.py` (rules, codes)
- **`src/dk_publishing/entrypoints/publish_api/llms.txt`** (what an AI agent reads) and `docs/api/README.md`
- Guide 02 (field table, error code list, examples, code samples) then `make guides`
- Tests in `tests/integration/test_api.py` (also keeps every sample the API hands out valid)
- Design change? ADR 0020 or a new ADR. Anything new an agent must send: update the "notes for an agent" list in guide 02

### A column or tab in the Google Sheet
- `config/sheet.yaml` (columns, notes, and the `readme:` text that is written into the Sheet's README tab)
- `src/dk_publishing/adapters/sheets/gateway.py` (read/write), `sheet_init.py` (formatting, validation), `src/dk_publishing/domain/sheet.py`
- `src/dk_publishing/application/use_cases/sync_sheet.py` (what is read and written back)
- Test doubles: `tests/support/memory_sheet.py`, `tests/support/fake_sheets.py`; tests `tests/unit/test_gateway.py`, `tests/unit/test_sheet_init.py`
- Guide 01 (every column must be explained, a test checks it) then `make guides`
- **On the live Sheet: run `make sheet-init`** (adds missing headers and formatting, never clears data)
- Remember the Sheets traps in `CLAUDE.md`: never clear a checkbox column, write only changed cells, `RAW` values

### A status or state (the lifecycle)
- `src/dk_publishing/domain/status.py` (`ALLOWED` moves), plus a **new migration** if the database `CHECK` lists it
- The API view `STATE` in `api_posts.py`, `row_status` in `src/dk_publishing/domain/sync.py`, the Calendar rules in `sync_sheet.py`
- The status list in `config/sheet.yaml` `readme:`, the status tables in guide 01 and guide 02
- Alerts in `src/dk_publishing/application/use_cases/alerts.py` if it should page the owner
- Tests: `tests/unit/test_status.py`, `tests/unit/test_lifecycle.py`

### The database
- A **new** file in `migrations/` (never edit an applied one; there is no downgrade; ADR 0015)
- The repository in `src/dk_publishing/adapters/persistence/`, its Protocol in `src/dk_publishing/application/ports.py`
- Any test double that implements the Protocol, `tests/integration/test_migrations.py`
- Deploy applies it automatically (the `migrate` service); never reset a database that holds live tokens or publish state

### Publishing, scheduling or retry behaviour
- Read ADR 0005, 0006, 0007 and the "publish ordering" bullet in `CLAUDE.md` first
- `src/dk_publishing/application/use_cases/publish.py`, `prepare.py`, `reconcile.py`, `schedule_native.py`
- Tests against real Postgres (`tests/integration/`), contract tests for adapters
- Never move the platform call inside a transaction or before the commit; never retry after a possibly-sent request

### Media, files, storage limits
- `src/dk_publishing/adapters/media/uploads.py` (intake), `drive_store.py` (Drive downloads), `janitor.py` (cleanup; its file-name rules must match the two stores)
- `src/dk_publishing/application/use_cases/media_retention.py` (grace period, thresholds) and ADR 0021
- Stated limits in guide 02, `llms.txt`, `docs/api/README.md` (a test checks the numbers)

### Approval flow (who can approve, where)
- `src/dk_publishing/application/use_cases/approve.py`, `review_drafts.py`, `api_posts.py`, `src/dk_publishing/application/use_cases/sync_sheet.py`
- ADR 0022; guide 01 ("Approve", "Posts that come from software"), guide 02 (keys and rights)
- Decision on record: own posts are approved with `ready`; agent posts in the Calendar. Do not merge them without asking

### A scheduled job, sensor or Dagster setting
- `src/dk_publishing/entrypoints/dagster_defs/` (`jobs.py`, `schedules.py`, `sensors.py`, `definitions.py`)
- `tests/integration/test_dagster.py` (the expected list of jobs and schedules), `dagster/dagster.prod.yaml` and `dagster.dev.yaml`
- No `from __future__ import annotations` in `jobs.py`/`sensors.py` (Dagster reads annotations at runtime)

### Settings, environment variables or `dk.json`
- `src/dk_publishing/composition.py`, `src/dk_publishing/adapters/platforms/dk_file.py` (the template and the env translation)
- `src/dk_publishing/adapters/platforms/setup_check.py`, the matching guide in `docs/setup/`, `docs/setup/update-secrets.md` if tokens
- Only `DATABASE_URL` and `DK_CONFIG_FILE` are real environment variables (a test enforces `.env.example`)

### Deployment, server or domain
- `deploy/compose.prod.yaml`, `deploy/Caddyfile`, `deploy/deploy.sh`, `Dockerfile`, `docs/setup/deploy.md`
- The shared server's Caddyfile (back it up, `caddy validate`, then reload). Compose project name must stay `dk-publishing`
- `deploy/deploy.sh` prunes build cache older than a day after every deploy (the disk is shared with the ordering system); keep that step

### Telegram messages or alerts
- `src/dk_publishing/application/use_cases/alerts.py`, `health.py`, `src/dk_publishing/adapters/notify/`
- `docs/setup/telegram.md`, ADR 0018, and what guide 01 says you will receive

## After merging (every time something user-visible or schema-related changed)

1. `make deploy` (applies migrations, restarts services, runs the in-container configuration check).
2. If the Sheet layout changed: `make sheet-init`, then check the Sheet once.
3. If a guide changed: `make guides` was run and the PDFs are committed (`docs/guides/pdf/`).
4. Look at the server for a few minutes (Telegram, Dagster at `localhost:3000` through the SSH tunnel).
5. Update `HANDOFF.md` if you are leaving open items for the next session.

## Keeping this map true

`tests/unit/test_docs_in_sync.py` checks that every file path written in backticks on this page exists. When you
add a new kind of change, add a row here. When a row stops being true, fix it in the same commit.
