from dagster import DefaultScheduleStatus, ScheduleDefinition

from dk_publishing.entrypoints.dagster_defs.jobs import (
    daily_digest,
    housekeeping,
    renew_tokens,
    sync_sheet_job,
    token_health,
)

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

daily_digest_schedule = ScheduleDefinition(
    name="daily_digest",
    job=daily_digest,
    cron_schedule="0 8 * * *",
    execution_timezone="Europe/Berlin",
    default_status=DefaultScheduleStatus.RUNNING,
)

# Tokens that expire are renewed once a week at night; the renewal itself skips a token that was
# refreshed recently, so running it daily is harmless and a failed night is retried the next.
token_renewal_schedule = ScheduleDefinition(
    name="token_renewal",
    job=renew_tokens,
    cron_schedule="30 3 * * *",
    execution_timezone="Europe/Berlin",
    default_status=DefaultScheduleStatus.RUNNING,
)

# After the renewal at 03:30, look at every login and permission once more, and warn on Telegram.
token_health_schedule = ScheduleDefinition(
    name="token_health",
    job=token_health,
    cron_schedule="45 3 * * *",
    execution_timezone="Europe/Berlin",
    default_status=DefaultScheduleStatus.RUNNING,
)
