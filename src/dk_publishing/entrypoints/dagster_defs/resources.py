from __future__ import annotations

from pathlib import Path

from dagster import ConfigurableResource

from dk_publishing import composition
from dk_publishing.application.ports import Notifier
from dk_publishing.application.services import Services, SyncServices


class ServicesResource(ConfigurableResource):  # type: ignore[type-arg]
    """The only bridge from Dagster to the application: it builds the use cases' dependencies."""

    database_url: str
    platforms_config: str = str(composition.DEFAULT_PLATFORMS_CONFIG)

    def renew_tokens(self) -> tuple[bool, str] | None:
        """(succeeded, message), or None when there is no renewable token to renew."""
        result = composition.renew_tokens(composition.environment())
        return None if result is None else (result.ok, result.message)

    def purge_public_media(self) -> int:
        return composition.purge_public_media(composition.environment())

    def services(self) -> Services:
        return composition.build_services(
            self.database_url, Path(self.platforms_config), env=composition.environment()
        )


class SheetSyncResource(ConfigurableResource):  # type: ignore[type-arg]
    """Google credentials and IDs for the Sheet sync. Left blank, they come from dk.json."""

    database_url: str
    credentials_path: str = ""
    sheet_id: str = ""
    folder_id: str = ""

    def _settings(self) -> tuple[dict[str, str], Path, str, str]:
        env = composition.environment()
        key = self.credentials_path or env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        return (
            env,
            Path(key).expanduser(),
            self.sheet_id or env.get("GOOGLE_SHEET_ID", ""),
            self.folder_id or env.get("GOOGLE_DRIVE_FOLDER_ID", ""),
        )

    def services(self) -> SyncServices:
        env, key, sheet_id, folder_id = self._settings()
        return composition.build_sync_services(self.database_url, key, sheet_id, folder_id, env=env)

    def modified_at(self) -> str:
        _, key, sheet_id, _ = self._settings()
        # The Sheet's own edit time, plus the last state change of any post, so a publish or a
        # failure reaches the Sheet within a couple of minutes rather than at the next fallback.
        return (
            f"{composition.sheet_modified_at(key, sheet_id)}"
            f"|{composition.last_variant_change(self.database_url)}"
        )


class NotifyResource(ConfigurableResource):  # type: ignore[type-arg]
    """Telegram and the heartbeat, from the environment. Everything here is optional: with no
    Telegram file configured the alerts are skipped, never fatal."""

    database_url: str

    def services(self) -> Services:
        return composition.build_alert_services(self.database_url)

    def notifier(self) -> Notifier | None:
        return composition.build_notifier(composition.environment())

    def warnings(self) -> list[str]:
        return composition.credential_warnings(composition.environment())

    def heartbeat(self) -> bool | None:
        return composition.ping_alive(composition.environment())
