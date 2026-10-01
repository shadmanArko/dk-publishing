from __future__ import annotations

from collections.abc import Mapping

from dk_publishing.application.ports import Publisher


class UnknownPlatform(KeyError):
    """No publisher is registered for this platform (it is off, or not built yet)."""


class StaticPublisherRegistry:
    def __init__(self, publishers: Mapping[str, Publisher]) -> None:
        self._publishers = dict(publishers)

    def for_platform(self, platform: str) -> Publisher:
        try:
            return self._publishers[platform]
        except KeyError:
            raise UnknownPlatform(platform) from None
