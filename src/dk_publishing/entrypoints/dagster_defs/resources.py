from __future__ import annotations

from pathlib import Path

from dagster import ConfigurableResource

from dk_publishing import composition
from dk_publishing.application.services import Services


class ServicesResource(ConfigurableResource):  # type: ignore[type-arg]
    """The only bridge from Dagster to the application: it builds the use cases' dependencies."""

    database_url: str
    platforms_config: str = str(composition.DEFAULT_PLATFORMS_CONFIG)

    def services(self) -> Services:
        return composition.build_services(self.database_url, Path(self.platforms_config))
