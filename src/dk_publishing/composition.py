"""Composition root: the only module allowed to construct adapters."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dk_publishing.adapters.clock import SystemClock
from dk_publishing.adapters.persistence.migrate import apply_migrations
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.application.ports import UnitOfWork
from dk_publishing.application.services import Services
from dk_publishing.domain.capabilities import Capabilities

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def migrate_database(database_url: str, directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    return apply_migrations(database_url, directory)


def unit_of_work(database_url: str) -> UnitOfWork:
    return PostgresUnitOfWork(database_url)


def dry_run_services(database_url: str, capabilities: Mapping[str, Capabilities]) -> Services:
    """Every platform in dry-run: all real except the final call, which goes to the ledger."""
    ledger = PostgresLedger(database_url)
    registry = StaticPublisherRegistry(
        {name: DryRunPublisher(name, caps, ledger) for name, caps in capabilities.items()}
    )
    return Services(
        uow=lambda: PostgresUnitOfWork(database_url), publishers=registry, clock=SystemClock()
    )
