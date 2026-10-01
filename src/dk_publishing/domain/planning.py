"""Decides what happens to a variant next and when. Pure: the caller supplies `now`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.status import VariantStatus

NATIVE_VERIFY_DELAY = timedelta(minutes=10)
BACKOFF = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=8))
MAX_JITTER = 0.25  # up to +25% on top of a backoff step


class Action(StrEnum):
    FETCH_MEDIA = "fetch_media"
    SCHEDULE_NATIVE = "schedule_native"
    PREPARE = "prepare"
    PUBLISH = "publish"
    RECONCILE = "reconcile"
    EXPIRE = "expire"


@dataclass(frozen=True, slots=True)
class NextStep:
    action: Action
    at: datetime


def deadline(publish_at: datetime, caps: Capabilities) -> datetime:
    """Past this instant the post is never published; it expires and alerts instead."""
    return publish_at + caps.max_lateness


def plan_next(
    *,
    status: VariantStatus,
    publish_at: datetime,
    caps: Capabilities,
    now: datetime,
    media_ready: bool = True,
    not_before: datetime | None = None,
) -> NextStep | None:
    """The one action due for a variant, or None if it is waiting on something else.

    `not_before` carries a retry delay. Whatever the rules say, no action is ever planned to run
    after the deadline: the variant expires instead.
    """
    if status is VariantStatus.UNKNOWN:
        return NextStep(Action.RECONCILE, now)
    if status is VariantStatus.SCHEDULED_NATIVE:
        return NextStep(Action.RECONCILE, publish_at + NATIVE_VERIFY_DELAY)
    if status is VariantStatus.PREPARED:
        step = NextStep(Action.PUBLISH, publish_at)
    elif status is VariantStatus.APPROVED:
        step = _approved_step(publish_at, caps, now, media_ready)
    else:
        return None

    due = step.at if not_before is None else max(step.at, not_before)
    limit = deadline(publish_at, caps)
    if max(due, now) > limit:
        return NextStep(Action.EXPIRE, limit)
    return NextStep(step.action, due)


def _approved_step(
    publish_at: datetime, caps: Capabilities, now: datetime, media_ready: bool
) -> NextStep:
    if not media_ready:
        return NextStep(Action.FETCH_MEDIA, now)
    if caps.native_window is not None:
        shortest, longest = caps.native_window
        if publish_at - now >= shortest:
            # Far enough ahead for the platform to hold it. If the slot is beyond the window,
            # wait until the window opens.
            return NextStep(Action.SCHEDULE_NATIVE, publish_at - longest)
    return NextStep(Action.PREPARE, publish_at - caps.prepare_lead)


def retry_at(
    *,
    now: datetime,
    limit: datetime,
    failures: int,
    retry_after: timedelta | None = None,
    jitter: float = 0.0,
) -> datetime | None:
    """When to retry after `failures` failed tries, or None to stop (give up or out of time).

    A platform-requested `retry_after` is obeyed as given and is not capped by the backoff steps.
    `jitter` is a caller-supplied number in [0, 1] so this stays deterministic.
    """
    if failures < 1:
        raise ValueError("failures counts the tries that already failed, so it starts at 1")
    if not 0.0 <= jitter <= 1.0:
        raise ValueError("jitter must be within [0, 1]")
    if retry_after is not None:
        delay = retry_after
    elif failures <= len(BACKOFF):
        step = BACKOFF[failures - 1]
        delay = step + step * (MAX_JITTER * jitter)
    else:
        return None
    when = now + delay
    return when if when <= limit else None
