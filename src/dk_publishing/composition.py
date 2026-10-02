"""Composition root: the only module allowed to construct adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from dk_publishing.adapters.clock import SystemClock
from dk_publishing.adapters.config.platforms import ConfigError, Mode, load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.adapters.media.drive_catalog import DriveCatalog, sheet_modified_time
from dk_publishing.adapters.media.drive_store import DriveMediaStore, GoogleDriveDownloader
from dk_publishing.adapters.notify.telegram import (
    DEFAULT_PATH as DEFAULT_TELEGRAM_PATH,
)
from dk_publishing.adapters.notify.telegram import (
    SetupResult,
    TelegramCredentials,
    ping_heartbeat,
    run_setup,
)
from dk_publishing.adapters.persistence.migrate import apply_migrations
from dk_publishing.adapters.persistence.rehearsal import create_rehearsal_drafts
from dk_publishing.adapters.persistence.sync import new_external_id
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.connect import ConnectResult, connect_from_env
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.adapters.platforms.live import build_live_publisher
from dk_publishing.adapters.platforms.meta_check import (
    MetaReport,
    SwapResult,
    check_meta,
    swap_for_page_token,
)
from dk_publishing.adapters.platforms.meta_credentials import (
    MetaCredentials,
    write_template_from_env,
)
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.adapters.platforms.threads_token import (
    RefreshResult,
    expiry_warnings,
    refresh_expiring_tokens,
)
from dk_publishing.adapters.sheets.gateway import GoogleSheetGateway
from dk_publishing.adapters.sheets.google_access import (
    AccessReport,
    check_access,
    connect,
    expected_missing_tabs,
)
from dk_publishing.adapters.sheets.sheet_init import POSTS, InitReport, initialise
from dk_publishing.application.ports import (
    AccountInfo,
    DuplicateAccount,
    Notifier,
    Publisher,
    UnitOfWork,
)
from dk_publishing.application.services import Services, SyncServices
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor, ActorKind
from dk_publishing.domain.timezones import local_to_serial

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
DEFAULT_PLATFORMS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "platforms.yaml"
DEFAULT_SHEET_CONFIG = Path(__file__).resolve().parents[2] / "config" / "sheet.yaml"
DEFAULT_MEDIA_DIR = Path(__file__).resolve().parents[2] / ".media"


def migrate_database(database_url: str, directory: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    return apply_migrations(database_url, directory)


def unit_of_work(database_url: str) -> UnitOfWork:
    return PostgresUnitOfWork(database_url)


def build_services(
    database_url: str,
    platforms_config: Path = DEFAULT_PLATFORMS_CONFIG,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Services:
    """Wire the platforms named in config. `env` carries what live adapters need (Page id, token
    file, Google key, media folder); without it only dry-run and off platforms can be built."""
    env = env or {}
    ledger = PostgresLedger(database_url)
    publishers: dict[str, Publisher] = {}
    live = False
    for name, settings in load_platforms(platforms_config).items():
        if settings.mode is Mode.OFF:
            continue
        if settings.mode is Mode.LIVE:
            publishers[name] = build_live_publisher(name, settings, env, transport)
            live = True
        elif settings.mode is Mode.DRY_RUN:
            publishers[name] = DryRunPublisher(
                name, settings.capabilities, ledger, clock=SystemClock()
            )
        else:
            raise ConfigError(
                f"platform {name!r} is {settings.mode.value!r} but no such adapter is built yet; "
                "set it to dry_run, live or off"
            )
    return Services(
        uow=lambda: PostgresUnitOfWork(database_url),
        publishers=StaticPublisherRegistry(publishers),
        clock=SystemClock(),
        media_store=_media_store(env) if live else None,
    )


def _media_store(env: Mapping[str, str]) -> DriveMediaStore:
    """Where live platforms get their files: downloaded from Drive into MEDIA_DIR."""
    key = env.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not key:
        raise ConfigError("a live platform needs GOOGLE_APPLICATION_CREDENTIALS to fetch media")
    _, drive, _, _ = connect(Path(key).expanduser())
    folder = Path(env.get("MEDIA_DIR", "").strip() or DEFAULT_MEDIA_DIR).expanduser()
    return DriveMediaStore(GoogleDriveDownloader(drive), folder)


def _rehearsal_services(database_url: str) -> Services:
    """Services holding dry-run publishers only, even if the config has live platforms."""
    ledger = PostgresLedger(database_url)
    publishers: dict[str, Publisher] = {
        name: DryRunPublisher(name, settings.capabilities, ledger, clock=SystemClock())
        for name, settings in load_platforms(DEFAULT_PLATFORMS_CONFIG).items()
        if settings.mode is Mode.DRY_RUN
    }
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
    """Create `count` posts spaced `spacing` apart, starting one spacing from now; approve them.

    A rehearsal never touches a live platform: only platforms in `dry_run` mode can be used,
    whatever the config says elsewhere, so it can never make a real post.
    """
    services = _rehearsal_services(database_url)
    dry = [
        name
        for name, settings in load_platforms(DEFAULT_PLATFORMS_CONFIG).items()
        if settings.mode is Mode.DRY_RUN
    ]
    if platforms is None:
        platforms = dry
    refused = [name for name in platforms if name not in dry]
    if refused:
        raise ConfigError(
            f"{', '.join(refused)}: not a dry-run platform, so a rehearsal cannot use it "
            "(live platforms post for real)"
        )
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
    database_url: str,
    credentials_path: Path,
    sheet_id: str,
    folder_id: str,
    tenant_id: str = "dk",
    env: Mapping[str, str] | None = None,
) -> SyncServices:
    """Everything the Sheet sync needs: the publishing services plus Google Sheet and Drive."""
    platforms = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    layout = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(platforms))
    sheets, drive, _, _ = connect(credentials_path)
    return SyncServices(
        core=build_services(database_url, env=env),
        sheet=GoogleSheetGateway(sheets, sheet_id=sheet_id, layout=layout, platforms=platforms),
        media=DriveCatalog(drive, folder_id),
        platforms=list(platforms),
        tenant_id=tenant_id,
    )


def sheet_modified_at(credentials_path: Path, sheet_id: str) -> str:
    _, drive, _, _ = connect(credentials_path)
    return sheet_modified_time(drive, sheet_id)


def add_account(
    database_url: str,
    platform: str,
    display_name: str,
    tenant_id: str = "dk",
    external_id: str | None = None,
) -> str:
    """Register an account. `external_id` is the platform's own account id (a Page id, a channel
    id); without one it is a placeholder for a dry-run account. The connect flow will create
    real ones."""
    if platform not in load_platforms(DEFAULT_PLATFORMS_CONFIG):
        raise ConfigError(f"unknown platform {platform!r}; see config/platforms.yaml")
    with PostgresUnitOfWork(database_url) as uow:
        account_id = uow.sync.add_account(
            tenant_id, platform, display_name.strip(), external_id or new_external_id(platform)
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


def meta_init(path: Path, env: Mapping[str, str]) -> bool:
    """Create the one-file Meta credentials template (no secrets in it). False if it exists."""
    return write_template_from_env(path, env)


def meta_check(path: Path, platforms_config: Path = DEFAULT_PLATFORMS_CONFIG) -> MetaReport:
    """Ask Meta what the credentials file's tokens can do. Never posts."""
    return check_meta(MetaCredentials(path), load_platforms(platforms_config))


def meta_page_token(path: Path, platforms_config: Path = DEFAULT_PLATFORMS_CONFIG) -> SwapResult:
    """Swap the user token in the credentials file for the Page's own token."""
    return swap_for_page_token(MetaCredentials(path), load_platforms(platforms_config))


def meta_refresh(
    path: Path, platforms_config: Path = DEFAULT_PLATFORMS_CONFIG, force: bool = False
) -> RefreshResult:
    """Renew the tokens that expire (and can be renewed) in the credentials file."""
    return refresh_expiring_tokens(
        MetaCredentials(path), load_platforms(platforms_config), force=force
    )


def connect_login(
    platform: str, env: Mapping[str, str], *, check_only: bool = False
) -> ConnectResult:
    """One-time login setup for a platform that needs it."""
    return connect_from_env(platform, env, check_only=check_only)


def telegram_path(env: Mapping[str, str]) -> Path:
    return Path(
        env.get("TELEGRAM_CREDENTIALS_FILE", "").strip() or DEFAULT_TELEGRAM_PATH
    ).expanduser()


def telegram_setup(command: str, env: Mapping[str, str]) -> SetupResult:
    """`init`, `chat`, `check` or `test` for the Telegram bot."""
    return run_setup(command, telegram_path(env))


def build_notifier(
    env: Mapping[str, str], transport: httpx.BaseTransport | None = None
) -> Notifier | None:
    """The Telegram notifier, or None when no credentials file is configured (alerts are then
    skipped, never fatal). A configured but incomplete file is an error worth stopping for."""
    if not env.get("TELEGRAM_CREDENTIALS_FILE", "").strip():
        return None
    return TelegramCredentials(telegram_path(env)).notifier(transport)


def build_alert_services(database_url: str) -> Services:
    """Alerts only read Postgres and the clock; no platform is touched."""
    return Services(
        uow=lambda: PostgresUnitOfWork(database_url),
        publishers=StaticPublisherRegistry({}),
        clock=SystemClock(),
    )


def credential_warnings(env: Mapping[str, str]) -> list[str]:
    """Tokens that need attention soon, in words, for the daily digest."""
    path = env.get("META_CREDENTIALS_FILE", "").strip()
    if not path or not Path(path).expanduser().exists():
        return []
    return expiry_warnings(MetaCredentials(Path(path)))


def ping_alive(env: Mapping[str, str]) -> bool | None:
    """Ping the external dead-man's switch. None when none is configured."""
    url = env.get("HEARTBEAT_URL", "").strip()
    return ping_heartbeat(url) if url else None
