from __future__ import annotations

import os
from pathlib import Path

from dagster import ConfigurableResource

from dk_publishing import composition
from dk_publishing.application.ports import Notifier
from dk_publishing.application.services import Services, SyncServices


class ServicesResource(ConfigurableResource):  # type: ignore[type-arg]
    """The only bridge from Dagster to the application: it builds the use cases' dependencies."""

    database_url: str
    platforms_config: str = str(composition.DEFAULT_PLATFORMS_CONFIG)

    def services(self) -> Services:
        return composition.build_services(
            self.database_url, Path(self.platforms_config), env=os.environ
        )


class SheetSyncResource(ConfigurableResource):  # type: ignore[type-arg]
    """Google credentials and IDs, from the environment, for the Sheet sync."""

    database_url: str
    credentials_path: str
    sheet_id: str
    folder_id: str

    def services(self) -> SyncServices:
        return composition.build_sync_services(
            self.database_url,
            Path(self.credentials_path).expanduser(),
            self.sheet_id,
            self.folder_id,
            env=os.environ,
        )

    def modified_at(self) -> str:
        return composition.sheet_modified_at(
            Path(self.credentials_path).expanduser(), self.sheet_id
        )


class NotifyResource(ConfigurableResource):  # type: ignore[type-arg]
    """Telegram and the heartbeat, from the environment. Everything here is optional: with no
    Telegram file configured the alerts are skipped, never fatal."""

    database_url: str

    def services(self) -> Services:
        return composition.build_alert_services(self.database_url)

    def notifier(self) -> Notifier | None:
        return composition.build_notifier(os.environ)

    def warnings(self) -> list[str]:
        return composition.credential_warnings(os.environ)

    def heartbeat(self) -> bool | None:
        return composition.ping_alive(os.environ)
