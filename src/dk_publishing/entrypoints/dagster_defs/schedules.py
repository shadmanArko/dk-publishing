from dagster import DefaultScheduleStatus, ScheduleDefinition

from dk_publishing.entrypoints.dagster_defs.jobs import housekeeping, sync_sheet_job

# Berlin time, so daylight saving never shifts a schedule.
housekeeping_schedule = ScheduleDefinition(
    name="housekeeping",
    job=housekeeping,
    cron_schedule="*/5 * * * *",
    execution_timezone="Europe/Berlin",
    default_status=DefaultScheduleStatus.RUNNING,
)

# A safety net: if a sync failed (Google hiccup) the sensor would not retry until the Sheet
# changed again. The sync is idempotent, so running it regularly is harmless.
sheet_sync_fallback = ScheduleDefinition(
    name="sheet_sync_fallback",
    job=sync_sheet_job,
    cron_schedule="*/15 * * * *",
    execution_timezone="Europe/Berlin",
    default_status=DefaultScheduleStatus.RUNNING,
    tags={"dk/sync": "sheet"},
)
