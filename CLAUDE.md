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
