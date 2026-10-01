from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.support import CAPS, MIN, NATIVE_CAPS, T0, H, ScriptedPublisher

from dk_publishing.application.use_cases._common import (
    delivery_of,
    delivery_problems,
    planning_caps,
)
from dk_publishing.domain.capabilities import for_delivery
from dk_publishing.domain.planning import Action, plan_next
from dk_publishing.domain.status import ALLOWED, VariantStatus

S = VariantStatus
SLOT = T0 + 6 * H


class FakePublisher:
    """Just enough of a publisher for the delivery checks."""

    def __init__(self, caps, native: bool) -> None:  # type: ignore[no-untyped-def]
        self.capabilities = caps
        if native:
            self.schedule: Callable[..., Any] = lambda *a, **k: {}
            self.cancel: Callable[..., None] = lambda *a, **k: None


# --- which capabilities the planner sees ------------------------------------------------------


def test_native_scheduling_is_used_only_when_the_row_asks_for_it() -> None:
    assert for_delivery(NATIVE_CAPS, "native") is NATIVE_CAPS
    for choice in ("direct", None, "", "anything else"):
        assert for_delivery(NATIVE_CAPS, choice).native_window is None
    assert for_delivery(CAPS, "native") == CAPS  # no window to keep or strip


def test_a_row_without_a_delivery_is_direct() -> None:
    assert delivery_of({"caption": "x"}) == "direct"
    assert delivery_of(None) == "direct" and delivery_of({"delivery": ""}) == "direct"
    assert delivery_of({"delivery": "native"}) == "native"


def plan(content: dict[str, str], now: datetime = T0):  # type: ignore[no-untyped-def]
    publisher = FakePublisher(NATIVE_CAPS, native=True)
    return plan_next(
        status=S.APPROVED,
        publish_at=SLOT,
        caps=planning_caps(publisher, content),  # type: ignore[arg-type]
        now=now,
    )


def test_a_direct_row_prepares_ahead_of_the_slot_as_before() -> None:
    step = plan({"caption": "x"})
    assert step is not None and (step.action, step.at) == (Action.PREPARE, SLOT - 5 * MIN)


def test_a_native_row_is_handed_to_the_platform_straight_away() -> None:
    step = plan({"delivery": "native"})
    assert step is not None and step.action is Action.SCHEDULE_NATIVE
    assert step.at <= T0  # the window (30 days) is already open, so it is due now


def test_a_native_row_far_in_the_future_waits_for_the_platforms_window() -> None:
    far = T0 + timedelta(days=90)
    publisher = FakePublisher(NATIVE_CAPS, native=True)
    step = plan_next(
        status=S.APPROVED,
        publish_at=far,
        caps=planning_caps(publisher, {"delivery": "native"}),  # type: ignore[arg-type]
        now=T0,
    )
    assert step is not None and (step.action, step.at) == (
        Action.SCHEDULE_NATIVE,
        far - timedelta(days=30),
    )


def test_a_native_row_too_close_to_the_slot_falls_back_to_publishing_directly() -> None:
    step = plan({"delivery": "native"}, now=SLOT - 4 * MIN)
    assert step is not None and step.action is Action.PREPARE


# --- what is checked before approval ----------------------------------------------------------


NATIVE = FakePublisher(NATIVE_CAPS, native=True)
NO_NATIVE = FakePublisher(CAPS, native=False)


@pytest.mark.parametrize(
    ("publisher", "content", "slot_in", "words"),
    [
        (NATIVE, {"delivery": "teleport"}, 6 * H, "must be one of: direct, native"),
        (NO_NATIVE, {"delivery": "native"}, 6 * H, "cannot hold scheduled posts"),
        (NATIVE, {"delivery": "native"}, 5 * MIN, "at least 10 minutes from now"),
    ],
)
def test_a_delivery_that_cannot_work_is_explained(publisher, content, slot_in, words) -> None:  # type: ignore[no-untyped-def]
    problems = delivery_problems(publisher, content, T0 + slot_in, T0)
    assert [p.field for p in problems] == ["delivery"] and words in problems[0].message


@pytest.mark.parametrize(
    ("publisher", "content", "slot_in"),
    [
        (NATIVE, {"delivery": "native"}, 6 * H),
        (NATIVE, {"delivery": "native"}, 10 * MIN),  # exactly the minimum is fine
        (NATIVE, {"delivery": "direct"}, 1 * MIN),  # direct has no minimum
        (NATIVE, {}, 1 * MIN),
        (NO_NATIVE, {"delivery": "direct"}, 6 * H),
    ],
)
def test_workable_deliveries_pass(publisher, content, slot_in) -> None:  # type: ignore[no-untyped-def]
    assert delivery_problems(publisher, content, T0 + slot_in, T0) == []


# --- the lifecycle -------------------------------------------------------------------------------


def test_a_post_the_platform_failed_to_publish_can_be_marked_failed() -> None:
    assert S.FAILED in ALLOWED[S.SCHEDULED_NATIVE]


def test_a_scheduled_post_still_cannot_reach_published_without_approval() -> None:
    from collections import deque

    seen, queue = {S.DRAFT}, deque([S.DRAFT])
    while queue:
        for nxt in ALLOWED[queue.popleft()]:
            if nxt not in seen and nxt is not S.APPROVED:
                seen.add(nxt)
                queue.append(nxt)
    assert S.PUBLISHED not in seen and S.SCHEDULED_NATIVE not in seen


def test_the_scripted_publisher_is_recognised_as_a_native_scheduler() -> None:
    from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, InMemoryLedger
    from dk_publishing.application.ports import NativeScheduler

    assert isinstance(
        ScriptedPublisher(DryRunPublisher("p", NATIVE_CAPS, InMemoryLedger())), NativeScheduler
    )
    assert not isinstance(NO_NATIVE, NativeScheduler)
    assert UTC and H
