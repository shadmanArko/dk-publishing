"""Which platform is handed to a person, and with what rules."""

from __future__ import annotations

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings
from dk_publishing.adapters.platforms.assisted import SentLog
from dk_publishing.adapters.platforms.tiktok import build_tiktok
from dk_publishing.application.ports import Notifier, Publisher


def build_assisted_publisher(
    name: str, settings: PlatformSettings, notifier: Notifier, sent: SentLog
) -> Publisher:
    if name == "tiktok":
        return build_tiktok(settings.capabilities, notifier, sent)
    raise ConfigError(f"platform {name!r} is assisted but has no hand-over rules yet")
