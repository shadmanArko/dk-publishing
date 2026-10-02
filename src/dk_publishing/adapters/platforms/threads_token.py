"""Threads access tokens last 60 days and can be refreshed once they are a day old."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from dk_publishing.adapters.config.platforms import PlatformSettings
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials, SectionTokenProvider
from dk_publishing.adapters.platforms.threads import DEFAULT_VERSION, HOST
from dk_publishing.domain.errors import PublishingError

REFRESH_AFTER = timedelta(days=7)  # refreshing weekly keeps a 60-day token far from expiry
WARN_WITHIN = timedelta(days=14)


@dataclass
class RefreshResult:
    ok: bool
    changed: bool
    message: str


def _graph(
    credentials: MetaCredentials, version: str, transport: httpx.BaseTransport | None
) -> GraphClient:
    return GraphClient(
        version=version,
        tokens=SectionTokenProvider(credentials, "threads"),
        transport=transport,
        host=HOST,
        video_host=HOST,
    )


def refresh_threads_token(
    credentials: MetaCredentials,
    *,
    version: str = DEFAULT_VERSION,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> RefreshResult:
    """Ask Threads for a fresh 60-day token and store it. Skipped when the current one was
    refreshed recently, unless `force`. The new token replaces the old one atomically."""
    now = now or datetime.now(UTC)
    section = credentials.section("threads")
    if not str(section.get("access_token") or "").strip():
        return RefreshResult(False, False, "threads.access_token is empty; paste a token first")
    recent = _when(section.get("refreshed_at"))
    if recent and not force and now - recent < REFRESH_AFTER:
        return RefreshResult(
            True, False, f"refreshed {(now - recent).days} days ago; nothing to do"
        )

    try:
        reply = _graph(credentials, version, transport).get(
            "refresh_access_token", {"grant_type": "th_refresh_token"}
        )
    except PublishingError as exc:
        return RefreshResult(False, False, f"Threads refused to refresh the token: {exc}")
    token, seconds = str(reply.get("access_token") or ""), int(reply.get("expires_in") or 0)
    if not token:
        return RefreshResult(False, False, "Threads answered without a new token")
    credentials.update_section(
        "threads",
        {
            "access_token": token,
            "refreshed_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=seconds)).isoformat() if seconds else "",
        },
    )
    days = seconds // 86400
    return RefreshResult(True, True, f"the Threads token was renewed and is good for {days} days")


def days_left(credentials: MetaCredentials, now: datetime | None = None) -> int | None:
    """Days until the stored Threads token expires, if that is known."""
    expires = _when(credentials.section("threads").get("expires_at"))
    return None if expires is None else (expires - (now or datetime.now(UTC))).days


def expiry_warnings(credentials: MetaCredentials, now: datetime | None = None) -> list[str]:
    """Plain-language warnings for tokens close to expiring, for the daily digest."""
    left = days_left(credentials, now)
    if left is None or left > WARN_WITHIN.days:
        return []
    when = "has expired" if left < 0 else f"expires in {left} days"
    return [f"The Threads token {when}; run `make meta-refresh` (or paste a new token)"]


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def refresh_expiring_tokens(
    credentials: MetaCredentials,
    platforms: Mapping[str, PlatformSettings],
    *,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> RefreshResult:
    """Renew every token that expires and can be renewed. Today that is the Threads token."""
    settings = platforms.get("threads")
    version = (settings.api_version if settings else None) or DEFAULT_VERSION
    return refresh_threads_token(credentials, version=version, force=force, transport=transport)
