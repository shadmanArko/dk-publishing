# ADR 0002: Postgres is both record and work queue

Status: accepted

## Decision

Postgres is both record and work queue.

## Rejected alternatives

Redis, RabbitMQ, Kafka.

## Why

Volume is tiny; transactions and audit live in one place.
