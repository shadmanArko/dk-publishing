"""Composition root: the only module allowed to construct adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dk_publishing.adapters.clock import SystemClock
from dk_publishing.adapters.config.platforms import ConfigError, Mode, load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.adapters.media.drive_catalog import DriveCatalog, sheet_modified_time
from dk_publishing.adapters.persistence.migrate import apply_migrations
from dk_publishing.adapters.persistence.rehearsal import create_rehearsal_drafts
from dk_publishing.adapters.persistence.sync import new_external_id
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.adapters.sheets.gateway import GoogleSheetGateway
from dk_publishing.adapters.sheets.google_access import (
    AccessReport,
    check_access,
    connect,
    expected_missing_tabs,
)
from dk_publishing.adapters.sheets.sheet_init import POSTS, InitReport, initialise
from dk_publishing.application.ports import AccountInfo, DuplicateAccount, Publisher, UnitOfWork
from dk_publishing.application.services import Services, SyncServices
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor, ActorKind
from dk_publishing.domain.timezones import local_to_serial

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


def build_sync_services(
    database_url: str, credentials_path: Path, sheet_id: str, folder_id: str, tenant_id: str = "dk"
) -> SyncServices:
    """Everything the Sheet sync needs: the publishing services plus Google Sheet and Drive."""
    platforms = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    layout = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(platforms))
    sheets, drive, _, _ = connect(credentials_path)
    return SyncServices(
        core=build_services(database_url),
        sheet=GoogleSheetGateway(sheets, sheet_id=sheet_id, layout=layout, platforms=platforms),
        media=DriveCatalog(drive, folder_id),
        platforms=list(platforms),
        tenant_id=tenant_id,
    )


def sheet_modified_at(credentials_path: Path, sheet_id: str) -> str:
    _, drive, _, _ = connect(credentials_path)
    return sheet_modified_time(drive, sheet_id)


def add_account(database_url: str, platform: str, display_name: str, tenant_id: str = "dk") -> str:
    """Register a (dry-run) account. Real accounts arrive with the connect flow."""
    if platform not in load_platforms(DEFAULT_PLATFORMS_CONFIG):
        raise ConfigError(f"unknown platform {platform!r}; see config/platforms.yaml")
    with PostgresUnitOfWork(database_url) as uow:
        account_id = uow.sync.add_account(
            tenant_id, platform, display_name.strip(), new_external_id(platform)
        )
        uow.commit()
    return account_id


def list_accounts(database_url: str, tenant_id: str = "dk") -> list[AccountInfo]:
    with PostgresUnitOfWork(database_url) as uow:
        return uow.sync.accounts(tenant_id)


def install_sample(
    database_url: str, credentials_path: Path, sheet_id: str, folder_id: str, tenant_id: str = "dk"
) -> list[str]:
    """Add the sample post and a dry-run account per platform; returns what was done."""
    platforms = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    layout = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(platforms))
    sample = layout.sample
    if not sample:
        raise ConfigError("config/sheet.yaml has no sample section")
    done: list[str] = []
    for key, row in (sample.get("rows") or {}).items():
        try:
            add_account(database_url, key, str(row["account"]), tenant_id)
            done.append(f"added the dry-run account {row['account']!r} for {key}")
        except DuplicateAccount:
            pass

    sheets, _, _, _ = connect(credentials_path)
    gateway = GoogleSheetGateway(sheets, sheet_id=sheet_id, layout=layout, platforms=platforms)
    snapshot = gateway.read()
    post = dict(sample["post"])
    if any(p.post_key == post["post_key"] for p in snapshot.posts):
        done.append(f"{post['post_key']} is already on the Posts tab; left it alone")
        return done
    if snapshot.problems:
        raise ConfigError("; ".join(snapshot.problems))

    gateway.append_row(POSTS, _sample_cells(post))
    done.append(f"added {post['post_key']} to the Posts tab")
    for key, row in (sample.get("rows") or {}).items():
        values = {"post_key": post["post_key"], **row}
        gateway.append_row(platforms[key].tab, _sample_cells(values))
        done.append(f"added a row to {platforms[key].tab}")
    return done


def _sample_cells(cells: Mapping[str, Any]) -> dict[str, Any]:
    """Sample values as they must be stored: dates as Sheet serials, everything else as typed."""
    out: dict[str, Any] = {}
    for name, value in cells.items():
        if name.endswith("slot") and isinstance(value, str):
            out[name] = local_to_serial(datetime.strptime(value, "%Y-%m-%d %H:%M"))
        else:
            out[name] = value
    return out
