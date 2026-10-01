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
  `dk sync --allow-cancellations`. Telegram will replace the failed-run signal later.
- `dk sheet sample` / `dk account add` create dry-run accounts; real ones come from the (unbuilt) connect flow.
