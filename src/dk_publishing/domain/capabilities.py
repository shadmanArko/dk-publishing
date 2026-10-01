"""What a platform can do, declared as data so the scheduler never branches on a platform name."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

ZERO = timedelta(0)


@dataclass(frozen=True, slots=True)
class Capabilities:
    native_window: tuple[timedelta, timedelta] | None  # min and max lead for native scheduling
    prepare_lead: timedelta  # start preparing this long before the slot
    prepared_ttl: timedelta | None  # how long a prepared handle stays valid
    pulls_media_by_url: bool  # platform fetches media from a public URL
    max_lateness: timedelta  # past slot + this, never publish; expire instead

    def __post_init__(self) -> None:
        if self.native_window is not None:
            low, high = self.native_window
            if not ZERO <= low <= high:
                raise ValueError(f"native_window must satisfy 0 <= min <= max, got {low}..{high}")
        if self.prepare_lead < ZERO:
            raise ValueError("prepare_lead must not be negative")
        if self.max_lateness <= ZERO:
            raise ValueError("max_lateness must be positive")
        if self.prepared_ttl is not None and self.prepare_lead >= self.prepared_ttl:
            raise ValueError("prepare_lead must stay below the prepared handle's lifetime")


def for_delivery(caps: Capabilities, delivery: str | None) -> Capabilities:
    """The capabilities the planner should use for one variant.

    Native scheduling (the platform holds the post and publishes it) happens only when the row asks
    for `delivery: native`. Anything else is published by this system at the slot.
    """
    return caps if delivery == "native" else replace(caps, native_window=None)
