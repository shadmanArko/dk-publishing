# ADR 0013: Separate repository, shared VPS and Postgres instance

Status: accepted (2026-10-01)

## Decision

`dk-publishing` is its own repository, deployed on the same VPS (Contabo) as the AI harness. It uses its own
schema, its own database role and its own Dagster instance. The harness gets a read-only role on the
publishing views and reads them for analytics. Agents (later) write posts with `posts.source = agent`.

## Rejected alternatives

A folder inside `dhaka_kacchi_ai_harness`.

## Why

Publishing makes irreversible public side effects and holds live OAuth tokens. The harness's `make reset` and
`verify-idempotent` run `alembic downgrade base`, which must never sit near live publish state. The two
projects also differ in toolchain, migration style, release cadence and deploy gating.
