from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dk_publishing.domain.timezones import (
    BERLIN,
    InvalidLocalTime,
    berlin_to_utc,
    ensure_utc,
    utc_to_berlin,
)


def test_summer_and_winter_offsets() -> None:
    assert berlin_to_utc(datetime(2026, 7, 1, 18, 0)) == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    assert berlin_to_utc(datetime(2026, 11, 14, 18, 0)) == datetime(2026, 11, 14, 17, 0, tzinfo=UTC)


def test_the_skipped_hour_in_spring_is_rejected() -> None:
    with pytest.raises(InvalidLocalTime, match="does not exist"):
        berlin_to_utc(datetime(2026, 3, 29, 2, 30))


def test_the_repeated_hour_in_autumn_is_rejected() -> None:
    with pytest.raises(InvalidLocalTime, match="happens twice"):
        berlin_to_utc(datetime(2026, 10, 25, 2, 30))


def test_either_side_of_the_boundary_hours_is_fine() -> None:
    assert berlin_to_utc(datetime(2026, 3, 29, 3, 0)) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert berlin_to_utc(datetime(2026, 10, 25, 3, 0)) == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    assert berlin_to_utc(datetime(2026, 10, 25, 1, 59)) == datetime(
        2026, 10, 24, 23, 59, tzinfo=UTC
    )


def test_aware_input_to_berlin_to_utc_is_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        berlin_to_utc(datetime(2026, 7, 1, 18, 0, tzinfo=UTC))


def test_naive_datetimes_are_never_guessed() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 7, 1, 18, 0))


def test_ensure_utc_converts_other_offsets() -> None:
    plus_two = datetime(2026, 7, 1, 18, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ensure_utc(plus_two) == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)


def _last_sunday(year: int, month: int) -> date:
    day = date(year, month, 31)
    return day - timedelta(days=(day.weekday() + 1) % 7)


@given(st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2036, 1, 1)))
def test_berlin_round_trips_and_only_the_clock_change_hours_are_rejected(local: datetime) -> None:
    try:
        utc = berlin_to_utc(local)
    except InvalidLocalTime:
        change_days = {_last_sunday(local.year, 3), _last_sunday(local.year, 10)}
        assert local.date() in change_days
        assert local.hour == 2
        return
    assert utc_to_berlin(utc).replace(tzinfo=None) == local
    assert utc_to_berlin(utc).tzinfo == BERLIN
