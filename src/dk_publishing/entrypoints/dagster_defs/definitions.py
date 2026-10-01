from __future__ import annotations

from dagster import Definitions, EnvVar

from dk_publishing.entrypoints.dagster_defs.jobs import (
    expire_variant_job,
    housekeeping,
    prepare_variant_job,
    publish_variant_job,
    reconcile_variant_job,
    sync_sheet_job,
)
from dk_publishing.entrypoints.dagster_defs.resources import ServicesResource, SheetSyncResource
from dk_publishing.entrypoints.dagster_defs.schedules import (
    housekeeping_schedule,
    sheet_sync_fallback,
)
from dk_publishing.entrypoints.dagster_defs.sensors import due_actions, sheet_changed


def default_sheet_sync() -> SheetSyncResource:
    return SheetSyncResource(
        database_url=EnvVar("DATABASE_URL"),
        credentials_path=EnvVar("GOOGLE_APPLICATION_CREDENTIALS"),
        sheet_id=EnvVar("GOOGLE_SHEET_ID"),
        folder_id=EnvVar("GOOGLE_DRIVE_FOLDER_ID"),
    )


def build_definitions(
    services: ServicesResource, sheet_sync: SheetSyncResource | None = None
) -> Definitions:
    return Definitions(
        jobs=[
            prepare_variant_job,
            publish_variant_job,
            reconcile_variant_job,
            expire_variant_job,
            housekeeping,
            sync_sheet_job,
        ],
        sensors=[due_actions, sheet_changed],
        schedules=[housekeeping_schedule, sheet_sync_fallback],
        resources={"services": services, "sheet_sync": sheet_sync or default_sheet_sync()},
    )


defs = build_definitions(ServicesResource(database_url=EnvVar("DATABASE_URL")))
