from collections.abc import Iterator

from dagster import (
    DefaultSensorStatus,
    JobDefinition,
    RunConfig,
    RunFailureSensorContext,
    RunRequest,
    SensorEvaluationContext,
    SkipReason,
    run_failure_sensor,
    sensor,
)

from dk_publishing.application.ports import DueAction, NotifyError
from dk_publishing.application.use_cases.alerts import send_alerts, send_run_failure
from dk_publishing.domain.planning import Action
from dk_publishing.entrypoints.dagster_defs.jobs import (
    ActionConfig,
    expire_variant_job,
    prepare_variant_job,
    publish_variant_job,
    reconcile_variant_job,
    schedule_native_job,
    sync_sheet_job,
)
from dk_publishing.entrypoints.dagster_defs.resources import (
    NotifyResource,
    ServicesResource,
    SheetSyncResource,
)

BATCH = 50

JOB_FOR_ACTION: dict[Action, JobDefinition] = {
    Action.PREPARE: prepare_variant_job,
    Action.SCHEDULE_NATIVE: schedule_native_job,
    Action.PUBLISH: publish_variant_job,
    Action.RECONCILE: reconcile_variant_job,
    Action.EXPIRE: expire_variant_job,
}  # fetch_media has no job until the media pipeline slice


def run_request(due: DueAction) -> RunRequest:
    """The run key is the idempotency boundary: one launch per action at a given version."""
    job = JOB_FOR_ACTION[due.action]
    return RunRequest(
        run_key=f"{due.action.value}:{due.variant_id}:{due.version}",
        job_name=job.name,
        run_config=RunConfig(
            ops={f"{job.name}_op": ActionConfig(variant_id=due.variant_id, version=due.version)}
        ),
        tags={
            "dk/account": due.account_id,  # dagster.yaml allows one run per account at a time
            "dk/platform": due.platform,
            "dk/variant": due.variant_id,
        },
    )


@sensor(
    jobs=list(JOB_FOR_ACTION.values()),
    minimum_interval_seconds=30,
    default_status=DefaultSensorStatus.RUNNING,
)
def due_actions(
    context: SensorEvaluationContext, services: ServicesResource
) -> Iterator[RunRequest | SkipReason]:
    svc = services.services()
    with svc.uow() as uow:
        due = uow.variants.due(svc.clock.now(), BATCH)
    launchable = [d for d in due if d.action in JOB_FOR_ACTION]
    for item in due:
        if item.action not in JOB_FOR_ACTION:
            context.log.warning(f"{item.action.value} for {item.variant_id} has no job yet")
    if not launchable:
        yield SkipReason("nothing due")
    for item in launchable:
        yield run_request(item)


@sensor(
    job=sync_sheet_job,
    minimum_interval_seconds=120,
    default_status=DefaultSensorStatus.RUNNING,
)
def sheet_changed(
    context: SensorEvaluationContext, sheet_sync: SheetSyncResource
) -> Iterator[RunRequest | SkipReason]:
    """Start a sync when Drive says the Sheet was modified. Our own write-back also bumps the
    modified time, which costs one extra sync that finds nothing to do and writes nothing."""
    modified = sheet_sync.modified_at()
    if modified == context.cursor:
        yield SkipReason("the Sheet has not changed")
        return
    context.update_cursor(modified)
    yield RunRequest(run_key=f"sync:{modified}", tags={"dk/sync": "sheet"})


@sensor(minimum_interval_seconds=60, default_status=DefaultSensorStatus.RUNNING)
def notifications(context: SensorEvaluationContext, notify: NotifyResource) -> Iterator[SkipReason]:
    """Send pending failure alerts and ping the dead-man's switch. Runs inside the sensor tick
    (no job per minute); a Telegram outage only delays alerts, they are sent once it is back."""
    alive = notify.heartbeat()
    if alive is False:
        context.log.warning("the heartbeat URL did not answer")
    notifier = notify.notifier()
    if notifier is None:
        yield SkipReason("Telegram is not configured")
        return
    report = send_alerts(notify.services(), notifier)
    for problem in report.failed:
        context.log.warning(f"alert not delivered, will retry: {problem}")
    yield SkipReason(f"sent {report.sent} alert(s)")


@run_failure_sensor(default_status=DefaultSensorStatus.RUNNING, minimum_interval_seconds=60)
def failed_run_alert(context: RunFailureSensorContext, notify: NotifyResource) -> None:
    """A halted sync or a crashed job tells a person; the next failure of the same job within the
    hour is not repeated."""
    notifier = notify.notifier()
    if notifier is None:
        return
    run = context.dagster_run
    try:
        send_run_failure(
            notify.services(),
            notifier,
            run_id=run.run_id,
            job=run.job_name,
            error=_why(context),
        )
    except NotifyError as exc:
        context.log.warning(f"could not send the failure alert: {exc}")


def _why(context: RunFailureSensorContext) -> str:
    """The step's own error ("the sync was halted: ..."), not Dagster's generic run message."""
    for event in context.get_step_failure_events():
        failure = event.step_failure_data
        if failure.error is not None:
            return failure.error.message.strip()
    return context.failure_event.message or "failed"
