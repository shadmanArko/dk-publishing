# ADR 0006: At-most-once publishing with reconciliation

Status: accepted

## Decision

At-most-once publishing with reconciliation.

## Rejected alternatives

Blind retries.

## Why

Platforms are not idempotent; a blind retry creates a duplicate.
