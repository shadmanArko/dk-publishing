"""The Sheet as the domain sees it: typed rows in, status cells out. No Google types here."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dk_publishing.domain.publishing import Violation


@dataclass(frozen=True, slots=True)
class PostRow:
    """One row of the Posts tab. Slots are naive Berlin wall-clock time, exactly as typed."""

    row: int  # 1-based row number in the Sheet
    post_key: str
    title: str
    media: tuple[str, ...]
    default_caption: str
    default_slot: datetime | None
    ready: bool
    problems: tuple[Violation, ...] = ()
    system: Mapping[str, Any] = field(default_factory=dict)  # status / last_error as read


@dataclass(frozen=True, slots=True)
class PlatformRow:
    """One row of a platform tab."""

    tab: str
    platform: str
    row: int
    post_key: str
    enabled: bool
    account: str
    caption: str
    slot: datetime | None
    options: Mapping[str, Any]  # the platform's own columns (format, privacy_level, ...)
    problems: tuple[Violation, ...] = ()
    system: Mapping[str, Any] = field(
        default_factory=dict
    )  # status / live_url / last_error / synced_at


@dataclass(frozen=True, slots=True)
class RawRow:
    """A row exactly as read, for the append-only audit copy in sheet_snapshots."""

    tab: str
    row: int
    cells: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CalendarDecision:
    """A person ticked `approve` or `reject` on a Calendar row (a post an agent submitted)."""

    row: int
    post_key: str
    platform: str
    account: str
    approve: bool
    reject: bool


@dataclass(slots=True)
class SheetSnapshot:
    decisions: list[CalendarDecision] = field(default_factory=list)
    posts: list[PostRow] = field(default_factory=list)
    rows: list[PlatformRow] = field(default_factory=list)
    raw: list[RawRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # tab-level, e.g. a column is missing


@dataclass(frozen=True, slots=True)
class MediaFile:
    id: str
    name: str
    md5: str | None
    mime: str | None = None
    size: int | None = None


# What goes back into the Sheet ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowStatus:
    tab: str
    row: int
    status: str
    live_url: str
    last_error: str
    synced_at: datetime  # aware UTC


@dataclass(frozen=True, slots=True)
class PostStatus:
    row: int
    status: str
    last_error: str


@dataclass(frozen=True, slots=True)
class CalendarEntry:
    slot: datetime  # aware UTC
    platform: str
    account: str
    post_key: str
    title: str
    status: str
    source: str
    format: str = ""
    caption: str = ""
    media: str = ""
    by: str = ""  # the API key behind an agent's post
    note: str = ""  # why something needs a person, or why a tick was refused
    watch: str = ""  # an agent post's link to the review page for its day (ADR 0025)


@dataclass(frozen=True, slots=True)
class StatusPlan:
    rows: Sequence[RowStatus] = ()
    posts: Sequence[PostStatus] = ()
    calendar: Sequence[CalendarEntry] | None = None  # None: leave the Calendar tab alone
    accounts: Mapping[str, Sequence[str]] | None = None  # platform -> display names
