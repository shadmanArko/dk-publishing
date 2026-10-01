# ADR 0015: Forward-only SQL migrations, no downgrade

Status: accepted (2026-10-01)

## Decision

Schema changes are numbered `migrations/NNNN_name.sql` files applied by a small runner
(`adapters/persistence/migrate.py`). Each file runs in its own transaction, is checksummed, and an applied
file can never be edited. There are no down-migrations. Changes follow expand-then-contract, so the previous
release runs against the new schema and a rollback is a redeploy.

## Rejected alternatives

Alembic with `downgrade()`.

## Why

The AI harness lost state once to `alembic downgrade base` against a dev database holding real data
(its CLAUDE.md, 2026-09-23). This database will hold live OAuth tokens and publish state, so the operation
that destroys it is removed instead of guarded. Test databases are rebuilt from scratch, never rewound.

## Consequences

- Platform names are not in any CHECK constraint, so a ninth platform needs no migration.
- `variant_events` and `sheet_snapshots` are append-only by trigger; an attempt is written once before a
  platform call and completed once after; `published` events require an earlier `approved` event.
- SQL CHECK lists duplicate the domain enums. `test_sql_checks_agree_with_the_domain_enums` fails on drift.
- Database roles (vault-only access to `credentials`) are cluster-level and are created at deploy time.
