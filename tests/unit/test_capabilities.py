from datetime import timedelta

import pytest

from dk_publishing.domain.capabilities import Capabilities

H = timedelta(hours=1)


def caps(**over: object) -> Capabilities:
    base: dict[str, object] = {
        "native_window": None,
        "prepare_lead": 30 * timedelta(minutes=1),
        "prepared_ttl": 24 * H,
        "pulls_media_by_url": False,
        "max_lateness": 2 * H,
    }
    return Capabilities(**{**base, **over})  # type: ignore[arg-type]


def test_valid_capabilities_build() -> None:
    assert caps(native_window=(timedelta(minutes=10), timedelta(days=30))).max_lateness == 2 * H


@pytest.mark.parametrize(
    "bad",
    [
        {"native_window": (timedelta(days=1), timedelta(minutes=1))},
        {"native_window": (-H, H)},
        {"prepare_lead": -H},
        {"max_lateness": timedelta(0)},
        {"prepared_ttl": timedelta(minutes=10)},
    ],
)
def test_nonsense_capabilities_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        caps(**bad)
