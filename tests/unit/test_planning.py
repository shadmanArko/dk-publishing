from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.planning import (
    NATIVE_VERIFY_DELAY,
    Action,
    NextStep,
    deadline,
    plan_next,
    retry_at,
)
from dk_publishing.domain.status import VariantStatus

S = VariantStatus
MIN = timedelta(minutes=1)
H = timedelta(hours=1)
SLOT = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)

PREPARE_PATH = Capabilities(None, 30 * MIN, 24 * H, True, 2 * H)
NATIVE_PATH = Capabilities((10 * MIN, timedelta(days=30)), 5 * MIN, None, False, 2 * H)


def plan(
    caps: Capabilities,
    now: datetime,
    status: S = S.APPROVED,
    *,
    media_ready: bool = True,
    not_before: datetime | None = None,
) -> NextStep | None:
    return plan_next(
        status=status,
        publish_at=SLOT,
        caps=caps,
        now=now,
        media_ready=media_ready,
        not_before=not_before,
    )


def must_plan(
    caps: Capabilities,
    now: datetime,
    status: S = S.APPROVED,
    *,
    media_ready: bool = True,
    not_before: datetime | None = None,
) -> NextStep:
    step = plan(caps, now, status, media_ready=media_ready, not_before=not_before)
    assert step is not None
    return step


def test_prepare_path_prepares_ahead_of_the_slot() -> None:
    step = must_plan(PREPARE_PATH, SLOT - 5 * H)
    assert (step.action, step.at) == (Action.PREPARE, SLOT - 30 * MIN)


def test_prepared_publishes_at_the_slot() -> None:
    step = must_plan(PREPARE_PATH, SLOT - 10 * MIN, S.PREPARED)
    assert (step.action, step.at) == (Action.PUBLISH, SLOT)


def test_native_path_schedules_at_once_inside_the_window() -> None:
    now = SLOT - 3 * H
    step = must_plan(NATIVE_PATH, now)
    assert (step.action, step.at) == (Action.SCHEDULE_NATIVE, SLOT - timedelta(days=30))
    assert step.at <= now  # already due


def test_native_path_waits_for_the_window_to_open() -> None:
    now = SLOT - timedelta(days=60)
    step = must_plan(NATIVE_PATH, now)
    assert (step.action, step.at) == (Action.SCHEDULE_NATIVE, SLOT - timedelta(days=30))
    assert step.at > now


def test_slot_closer_than_the_window_falls_back_to_the_prepare_path() -> None:
    step = must_plan(NATIVE_PATH, SLOT - 4 * MIN)
    assert step.action is Action.PREPARE


def test_media_is_fetched_before_anything_else() -> None:
    now = SLOT - H
    step = must_plan(PREPARE_PATH, now, media_ready=False)
    assert (step.action, step.at) == (Action.FETCH_MEDIA, now)


def test_natively_scheduled_variants_are_verified_after_the_slot() -> None:
    step = must_plan(NATIVE_PATH, SLOT - H, S.SCHEDULED_NATIVE)
    assert (step.action, step.at) == (Action.RECONCILE, SLOT + NATIVE_VERIFY_DELAY)


def test_unknown_is_reconciled_immediately() -> None:
    now = SLOT + 5 * MIN
    step = must_plan(PREPARE_PATH, now, S.UNKNOWN)
    assert (step.action, step.at) == (Action.RECONCILE, now)


@pytest.mark.parametrize(
    "status",
    [
        *(S.DRAFT, S.INVALID, S.PENDING_APPROVAL),
        *(S.PREPARING, S.SCHEDULING_NATIVE, S.PUBLISHING),
        *(S.PUBLISHED, S.FAILED, S.CANCELLED, S.EXPIRED),
    ],
)
def test_other_states_have_no_scheduled_action(status: S) -> None:
    assert plan(PREPARE_PATH, SLOT - H, status) is None


def test_a_missed_deadline_expires_instead_of_publishing() -> None:
    now = SLOT + 3 * H
    step = must_plan(PREPARE_PATH, now, S.PREPARED)
    assert (step.action, step.at) == (Action.EXPIRE, deadline(SLOT, PREPARE_PATH))


def test_just_inside_the_deadline_still_publishes() -> None:
    now = SLOT + 2 * H
    assert must_plan(PREPARE_PATH, now, S.PREPARED).action is Action.PUBLISH


def test_a_retry_delay_past_the_deadline_expires() -> None:
    now = SLOT + MIN
    step = must_plan(PREPARE_PATH, now, S.PREPARED, not_before=SLOT + 3 * H)
    assert step.action is Action.EXPIRE


def test_a_retry_delay_inside_the_deadline_is_honoured() -> None:
    now = SLOT + MIN
    later = SLOT + 10 * MIN
    step = must_plan(PREPARE_PATH, now, S.PREPARED, not_before=later)
    assert (step.action, step.at) == (Action.PUBLISH, later)


capabilities = st.builds(
    lambda window, lead, late: Capabilities(window, lead, None, False, late),
    st.none() | st.tuples(st.just(10 * MIN), st.integers(11, 60 * 24 * 30).map(lambda m: m * MIN)),
    st.integers(0, 600).map(lambda m: m * MIN),
    st.integers(1, 600).map(lambda m: m * MIN),
)


@given(
    capabilities,
    st.integers(-3 * 24 * 60, 60 * 24 * 60).map(lambda m: SLOT - m * MIN),
    st.sampled_from([S.APPROVED, S.PREPARED]),
    st.booleans(),
    st.none() | st.integers(0, 24 * 60).map(lambda m: SLOT + m * MIN),
)
def test_no_planned_action_ever_runs_after_the_deadline(
    caps: Capabilities, now: datetime, status: S, media_ready: bool, not_before: datetime | None
) -> None:
    step = plan_next(
        status=status,
        publish_at=SLOT,
        caps=caps,
        now=now,
        media_ready=media_ready,
        not_before=not_before,
    )
    assert step is not None
    limit = deadline(SLOT, caps)
    if step.action is Action.EXPIRE:
        assert step.at == limit
    else:
        assert max(step.at, now) <= limit


NOW = datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
FAR = NOW + 2 * H


def test_backoff_steps_are_30s_2min_8min_then_give_up() -> None:
    gaps = [retry_at(now=NOW, limit=FAR, failures=n) for n in (1, 2, 3, 4)]
    assert gaps == [NOW + timedelta(seconds=30), NOW + 2 * MIN, NOW + 8 * MIN, None]


def test_jitter_adds_up_to_a_quarter() -> None:
    assert retry_at(now=NOW, limit=FAR, failures=2, jitter=1.0) == NOW + timedelta(seconds=150)


def test_rate_limit_waits_exactly_as_told_and_ignores_the_attempt_cap() -> None:
    assert retry_at(now=NOW, limit=FAR, failures=9, retry_after=15 * MIN) == NOW + 15 * MIN


def test_nothing_retries_past_the_deadline() -> None:
    assert retry_at(now=NOW, limit=NOW + 10 * timedelta(seconds=1), failures=1) is None
    assert retry_at(now=NOW, limit=NOW + 10 * MIN, failures=1, retry_after=H) is None


@pytest.mark.parametrize(("failures", "jitter"), [(0, 0.0), (1, 1.5), (1, -0.1)])
def test_bad_retry_arguments_are_refused(failures: int, jitter: float) -> None:
    with pytest.raises(ValueError):
        retry_at(now=NOW, limit=FAR, failures=failures, jitter=jitter)
