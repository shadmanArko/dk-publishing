"""Composition root: the only module allowed to construct adapters."""

from __future__ import annotations

from pathlib import Path

from dk_publishing.adapters.persistence.migrate import apply_migrations
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.application.ports import UnitOfWork

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def migrate_database(database_url: str, directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    return apply_migrations(database_url, directory)


def unit_of_work(database_url: str) -> UnitOfWork:
    return PostgresUnitOfWork(database_url)
