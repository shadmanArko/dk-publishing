from __future__ import annotations

from types import TracebackType
from typing import Any, Self

import psycopg

from dk_publishing.adapters.persistence.attempts import PostgresAttemptRepository
from dk_publishing.adapters.persistence.variants import PostgresVariantRepository


class PostgresUnitOfWork:
    """One connection, one transaction. Nothing persists unless `commit()` is called."""

    variants: PostgresVariantRepository
    attempts: PostgresAttemptRepository

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo
        self._conn: psycopg.Connection[tuple[Any, ...]] | None = None

    def __enter__(self) -> Self:
        self._conn = psycopg.connect(self._conninfo, options="-c timezone=UTC")
        self.variants = PostgresVariantRepository(self._conn)
        self.attempts = PostgresAttemptRepository(self._conn)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._conn is not None
        try:
            self._conn.rollback()  # a no-op after commit(); undoes everything otherwise
        finally:
            self._conn.close()
            self._conn = None

    def commit(self) -> None:
        assert self._conn is not None
        self._conn.commit()
