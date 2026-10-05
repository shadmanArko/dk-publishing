from __future__ import annotations

from dagster import Definitions, EnvVar

from dk_publishing.entrypoints.dagster_defs.jobs import (
    daily_digest,
    expire_variant_job,
    housekeeping,
    prepare_variant_job,
    publish_variant_job,
    reconcile_variant_job,
    renew_tokens,
    schedule_native_job,
    sync_sheet_job,
    token_health,
)
from dk_publishing.entrypoints.dagster_defs.resources import (
    NotifyResource,
    ServicesResource,
    SheetSyncResource,
)
from dk_publishing.entrypoints.dagster_defs.schedules import (
    daily_digest_schedule,
    housekeeping_schedule,
    sheet_sync_fallback,
    token_health_schedule,
    token_renewal_schedule,
)
from dk_publishing.entrypoints.dagster_defs.sensors import (
    due_actions,
    failed_run_alert,
    notifications,
    sheet_changed,
)


def default_sheet_sync() -> SheetSyncResource:
    return SheetSyncResource(database_url=EnvVar("DATABASE_URL"))


def build_definitions(
    services: ServicesResource, sheet_sync: SheetSyncResource | None = None
) -> Definitions:
    return Definitions(
        jobs=[
            prepare_variant_job,
            publish_variant_job,
            reconcile_variant_job,
            schedule_native_job,
            expire_variant_job,
            housekeeping,
            sync_sheet_job,
            daily_digest,
            renew_tokens,
            token_health,
        ],
        sensors=[due_actions, sheet_changed, notifications, failed_run_alert],
        schedules=[
            housekeeping_schedule,
            sheet_sync_fallback,
            daily_digest_schedule,
            token_renewal_schedule,
            token_health_schedule,
        ],
        resources={
            "services": services,
            "sheet_sync": sheet_sync or default_sheet_sync(),
            "notify": NotifyResource(database_url=EnvVar("DATABASE_URL")),
        },
    )


defs = build_definitions(ServicesResource(database_url=EnvVar("DATABASE_URL")))
