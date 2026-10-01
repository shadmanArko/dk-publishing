"""Composition root: the only module allowed to construct adapters."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from dk_publishing.adapters.clock import SystemClock
from dk_publishing.adapters.config.platforms import ConfigError, Mode, load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.adapters.persistence.migrate import apply_migrations
from dk_publishing.adapters.persistence.rehearsal import create_rehearsal_drafts
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.adapters.sheets.google_access import (
    AccessReport,
    check_access,
    connect,
    expected_missing_tabs,
)
from dk_publishing.adapters.sheets.sheet_init import InitReport, initialise
from dk_publishing.application.ports import Publisher, UnitOfWork
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor, ActorKind

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_PLATFORMS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "platforms.yaml"
DEFAULT_SHEET_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sheet.yaml"


def migrate_database(database_url: str, directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    return apply_migrations(database_url, directory)


def unit_of_work(database_url: str) -> UnitOfWork:
    return PostgresUnitOfWork(database_url)


def build_services(
    database_url: str, platforms_config: Path = DEFAULT_PLATFORMS_CONFIG
) -> Services:
    """Wire the platforms named in config. Only `dry_run` has an adapter so far."""
    ledger = PostgresLedger(database_url)
    publishers: dict[str, Publisher] = {}
    for name, settings in load_platforms(platforms_config).items():
        if settings.mode is Mode.OFF:
            continue
        if settings.mode is not Mode.DRY_RUN:
            raise ConfigError(
                f"platform {name!r} is {settings.mode.value!r} but no such adapter is built yet; "
                "set it to dry_run or off"
            )
        # Native scheduling is not built, so a dry run uses the prepare-then-publish path.
        caps = replace(settings.capabilities, native_window=None)
        publishers[name] = DryRunPublisher(name, caps, ledger)
    return Services(
        uow=lambda: PostgresUnitOfWork(database_url),
        publishers=StaticPublisherRegistry(publishers),
        clock=SystemClock(),
    )


def seed_rehearsal(
    database_url: str,
    *,
    count: int,
    spacing: timedelta,
    platforms: Sequence[str] | None = None,
    tenant_id: str = "dk",
) -> list[RunResult]:
    """Create `count` posts spaced `spacing` apart, starting one spacing from now; approve them."""
    services = build_services(database_url)
    if platforms is None:  # every platform that is switched on in config
        platforms = [
            name
            for name, settings in load_platforms(DEFAULT_PLATFORMS_CONFIG).items()
            if settings.mode is Mode.DRY_RUN
        ]
    start = services.clock.now()
    slots = [start + spacing * n for n in range(1, count + 1)]
    drafts = create_rehearsal_drafts(
        database_url,
        tenant_id=tenant_id,
        platforms=platforms,
        slots=slots,
        key_prefix=f"REHEARSAL-{start:%Y%m%d-%H%M%S}",
    )
    actor = Actor(ActorKind.HUMAN, "rehearsal")
    results = []
    for draft in drafts:
        content = {"caption": f"Rehearsal post for {draft.platform}, slot {draft.publish_at:%H:%M}"}
        result, _ = approve(services, draft.id, content, actor)
        results.append(result)
    return results


def check_google(
    credentials_path: Path, sheet_id: str, folder_id: str
) -> tuple[AccessReport, list[str]]:
    """Probe the Sheet and Drive folder. Returns the report and any expected-but-absent tabs."""
    sheets, drive, email, warning = connect(credentials_path)
    report = check_access(sheets, drive, sheet_id=sheet_id, folder_id=folder_id, email=email)
    report.key_file_warning = warning
    tabs = [p.tab for p in load_platforms(DEFAULT_PLATFORMS_CONFIG).values()]
    return report, expected_missing_tabs(report, tabs)


def init_sheet(credentials_path: Path, sheet_id: str, *, dry_run: bool) -> InitReport:
    """Create or repair the Google Sheet layout. Never overwrites existing data."""
    platforms = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    layout = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(platforms))
    sheets, _, _, _ = connect(credentials_path)
    return initialise(sheets, sheet_id, layout, platforms, dry_run=dry_run)
