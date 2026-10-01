"""Time handling. Storage is UTC; Europe/Berlin exists only at the edges (Sheet, messages)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


class InvalidLocalTime(ValueError):
    """A wall-clock time that does not map to exactly one instant."""


def ensure_utc(moment: datetime) -> datetime:
    """Normalise an aware datetime to UTC. Naive datetimes are a bug, never guessed."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"naive datetime is not allowed: {moment!r}")
    return moment.astimezone(UTC)


def berlin_to_utc(local: datetime) -> datetime:
    """Convert Berlin wall-clock time to UTC, rejecting times that are skipped or repeated.

    On the spring-forward night 02:00-02:59 does not exist; on the autumn night it happens twice.
    Guessing which instant was meant could publish an hour early or late, so both are rejected.
    """
    if local.tzinfo is not None:
        raise ValueError(f"expected naive Berlin wall-clock time, got {local!r}")
    first = local.replace(tzinfo=BERLIN, fold=0)
    second = local.replace(tzinfo=BERLIN, fold=1)
    if first.utcoffset() != second.utcoffset():
        skipped = first.astimezone(UTC).astimezone(BERLIN).replace(tzinfo=None) != local
        kind = (
            "does not exist (clocks jump forward)" if skipped else "happens twice (clocks go back)"
        )
        raise InvalidLocalTime(f"{local:%d.%m.%Y %H:%M} {kind} in Berlin; pick another time")
    return first.astimezone(UTC)


def utc_to_berlin(moment: datetime) -> datetime:
    """Berlin wall-clock view of an instant, for display only."""
    return ensure_utc(moment).astimezone(BERLIN)
