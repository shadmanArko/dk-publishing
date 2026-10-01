from __future__ import annotations

import os
from pathlib import Path

from dagster import ConfigurableResource

from dk_publishing import composition
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
