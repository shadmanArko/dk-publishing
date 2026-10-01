# ADR 0012: Default run launcher in one code-location container; own Dagster instance (option A)

Status: accepted

## Decision

Default run launcher in one code-location container; own Dagster instance (option A).

## Rejected alternatives

Container per run; a code location inside the harness's Dagster.

## Why

Less overhead, no Docker socket, and a harness outage cannot stop publishing. Confirmed 2026-10-01.
