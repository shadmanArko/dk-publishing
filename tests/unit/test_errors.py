from datetime import timedelta

import pytest

from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimited(timedelta(seconds=90))
    assert err.retry_after == timedelta(seconds=90)
    assert "0:01:30" in str(err)


@pytest.mark.parametrize("cls", [Retryable, AuthFailed, Rejected, UnknownOutcome])
def test_every_outcome_is_a_publishing_error(cls: type[PublishingError]) -> None:
    assert issubclass(cls, PublishingError)
    assert issubclass(RateLimited, PublishingError)
