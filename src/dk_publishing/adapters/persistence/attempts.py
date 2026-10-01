from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg import errors

from dk_publishing.application.ports import DuplicateAttempt
from dk_publishing.domain.attempt import Outcome, Phase

Conn = psycopg.Connection[tuple[Any, ...]]


class PostgresAttemptRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def begin(
        self,
        *,
        tenant_id: str,
        variant_id: str,
        phase: Phase,
        idempotency_key: str,
        started_at: datetime,
    ) -> str:
        try:
            # A savepoint, so a duplicate key does not poison the caller's transaction.
            with self._conn.transaction():
                row = self._conn.execute(
                    """INSERT INTO publishing.publish_attempts
                           (tenant_id, variant_id, phase, idempotency_key, started_at)
                       VALUES (%s, %s::uuid, %s, %s, %s) RETURNING id::text""",
                    (tenant_id, variant_id, phase.value, idempotency_key, started_at),
                ).fetchone()
        except errors.UniqueViolation as exc:
            raise DuplicateAttempt(idempotency_key) from exc
        assert row is not None
        return str(row[0])

    def finish(
        self,
        attempt_id: str,
        *,
        outcome: Outcome,
        finished_at: datetime,
        error_code: str | None = None,
        http_status: int | None = None,
        response_excerpt: str | None = None,
    ) -> None:
        cursor = self._conn.execute(
            """UPDATE publishing.publish_attempts
               SET outcome = %s, finished_at = %s, error_code = %s,
                   http_status = %s, response_excerpt = %s
               WHERE id = %s::uuid""",
            (
                outcome.value,
                finished_at,
                error_code,
                http_status,
                None if response_excerpt is None else response_excerpt[:2000],
                attempt_id,
            ),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"no attempt {attempt_id}")

    def finish_open(
        self, variant_id: str, *, outcome: Outcome, finished_at: datetime, error_code: str
    ) -> int:
        cursor = self._conn.execute(
            """UPDATE publishing.publish_attempts
               SET outcome = %s, finished_at = %s, error_code = %s
               WHERE variant_id = %s::uuid AND outcome IS NULL""",
            (outcome.value, finished_at, error_code, variant_id),
        )
        return cursor.rowcount

    def failures(self, variant_id: str, phase: Phase) -> int:
        row = self._conn.execute(
            """SELECT count(*) FROM publishing.publish_attempts
               WHERE variant_id = %s::uuid AND phase = %s
                 AND outcome IN ('retryable', 'rate_limited', 'unknown')""",
            (variant_id, phase.value),
        ).fetchone()
        assert row is not None
        return int(row[0])
