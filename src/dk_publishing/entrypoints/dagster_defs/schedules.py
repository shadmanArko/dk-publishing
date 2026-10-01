from dagster import DefaultScheduleStatus, ScheduleDefinition

from dk_publishing.entrypoints.dagster_defs.jobs import housekeeping

# Berlin time, so daylight saving never shifts a schedule.
housekeeping_schedule = ScheduleDefinition(
    name="housekeeping",
    job=housekeeping,
    cron_schedule="*/5 * * * *",
    execution_timezone="Europe/Berlin",
    default_status=DefaultScheduleStatus.RUNNING,
)
