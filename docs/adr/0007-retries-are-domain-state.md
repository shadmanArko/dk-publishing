# ADR 0007: Retries are domain state

Status: accepted

## Decision

Retries are domain state.

## Rejected alternatives

Dagster RetryPolicy.

## Why

Retries must respect deadlines and the reconciliation rule.
