"""Forward-only SQL migrations.

There is deliberately no downgrade. Migrations are expand-then-contract, so the previous release
keeps working against the new schema and a rollback is a redeploy, never a schema rewind.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import psycopg

SCHEMA = "publishing"
LOCK_KEY = 7_283_901  # arbitrary; serialises concurrent migrators
FILENAME = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    """A migration cannot be applied safely."""


def apply_migrations(conninfo: str, directory: Path) -> list[str]:
    """Apply every unapplied migration in order. Returns the names applied this run."""
    files = sorted(p for p in directory.iterdir() if FILENAME.match(p.name))
    if not files:
        raise MigrationError(f"no migrations found in {directory}")

    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations (
                name       text        NOT NULL,
                checksum   text        NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT pk_schema_migrations PRIMARY KEY (name)
            )"""
        )
        rows = conn.execute(f"SELECT name, checksum FROM {SCHEMA}.schema_migrations").fetchall()
        done: dict[str, str] = {name: checksum for name, checksum in rows}

        seen_pending = False
        for path in files:
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if path.name in done:
                if done[path.name] != checksum:
                    raise MigrationError(
                        f"{path.name} was edited after it was applied; add a new migration instead"
                    )
                if seen_pending:
                    raise MigrationError(f"{path.name} is applied but an earlier migration is not")
                continue
            seen_pending = True
            with conn.transaction():
                conn.execute(sql.encode())
                conn.execute(
                    f"INSERT INTO {SCHEMA}.schema_migrations (name, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )
            applied_now.append(path.name)
    return applied_now
