"""One small job per action. An op is three lines: build, call, log. No business logic here."""

from collections.abc import Callable

from dagster import (
    Backoff,
    Config,
    Failure,
    JobDefinition,
    OpExecutionContext,
    RetryPolicy,
    job,
    op,
)

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.alerts import send_digest
from dk_publishing.application.use_cases.dispatch import expire_variant
from dk_publishing.application.use_cases.health import report_health
from dk_publishing.application.use_cases.housekeeping import (
    fail_stale_scheduling,
    flag_stale_publishing,
    recover_stale_preparing,
)
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.application.use_cases.schedule_native import schedule_native_variant
from dk_publishing.application.use_cases.sync_sheet import sync_sheet
from dk_publishing.entrypoints.dagster_defs.resources import (
    NotifyResource,
    ServicesResource,
    SheetSyncResource,
)


class ActionConfig(Config):
    variant_id: str
    version: int  # the variant's version when the action was due; part of the run key


def _action_job(name: str, use_case: Callable[[Services, str, int], RunResult]) -> JobDefinition:
    @op(name=f"{name}_op")
    def run(context: OpExecutionContext, config: ActionConfig, services: ServicesResource) -> None:
        result = use_case(services.services(), config.variant_id, config.version)
        context.log.info(f"{name} {config.variant_id} v{config.version}: {result.value}")

    @job(name=name)
    def action_job() -> None:
        run()

    return action_job


prepare_variant_job = _action_job("prepare_variant", prepare_variant)
schedule_native_job = _action_job("schedule_native", schedule_native_variant)
publish_variant_job = _action_job("publish_variant", publish_variant)
reconcile_variant_job = _action_job("reconcile_variant", reconcile_variant)
expire_variant_job = _action_job("expire_variant", expire_variant)


@op
def housekeeping_op(context: OpExecutionContext, services: ServicesResource) -> None:
    svc = services.services()
    uncertain, reprepared = flag_stale_publishing(svc), recover_stale_preparing(svc)
    stuck = fail_stale_scheduling(svc)
    if stuck:
        context.log.warning(f"runs died while scheduling; check the platform for copies: {stuck}")
    if uncertain:
        context.log.warning(f"runs died mid-publish, now being reconciled: {uncertain}")
    purged = services.purge_public_media()
    if purged:
        context.log.info(f"removed {purged} public media link(s) nobody revoked")
    context.log.info(f"housekeeping: {len(uncertain)} uncertain, {len(reprepared)} re-prepared")


@job
def housekeeping() -> None:
    housekeeping_op()


# A dropped connection to Google for a few seconds must not page anyone: the sync is safe to repeat
# (it only reads the Sheet and writes status text), so it retries before the run counts as failed.
SYNC_RETRY = RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL)


@op(retry_policy=SYNC_RETRY)
def sync_sheet_op(context: OpExecutionContext, sheet_sync: SheetSyncResource) -> None:
    report = sync_sheet(sheet_sync.services())
    context.log.info(
        f"sheet sync: created {report.created}, approved {report.approved}, invalid "
        f"{report.marked_invalid}, withdrawn {report.withdrawn}, cancelled {report.cancelled}, "
        f"wrote {report.cells_written} cells"
    )
    for problem in report.problems:
        context.log.warning(problem)
    if report.halted:
        # A person must look before anything is cancelled: retrying would only halt again.
        raise Failure(description=report.halted, allow_retries=False)
    if report.errors:
        raise Failure(description="; ".join(report.errors))


@job(name="sync_sheet")
def sync_sheet_job() -> None:
    sync_sheet_op()


@op
def daily_digest_op(context: OpExecutionContext, notify: NotifyResource) -> None:
    try:
        notifier = notify.notifier()
    except ConfigError as exc:
        context.log.warning(f"no digest sent, Telegram is incomplete: {exc}")
        return
    if notifier is None:
        context.log.info("Telegram is not configured; no digest sent")
        return
    sent = send_digest(notify.services(), notifier, notify.warnings())
    context.log.info("digest sent" if sent else "today's digest was already sent")


@job
def daily_digest() -> None:
    daily_digest_op()


@op
def renew_tokens_op(context: OpExecutionContext, services: ServicesResource) -> None:
    result = services.renew_tokens()
    if result is None:
        context.log.info("no renewable token is configured; nothing to do")
    elif not result[0]:
        raise Failure(description=f"could not renew a token: {result[1]}")
    else:
        context.log.info(result[1])


@job
def renew_tokens() -> None:
    renew_tokens_op()


@op
def token_health_op(context: OpExecutionContext, notify: NotifyResource) -> None:
    failures = notify.health_failures()
    if not failures:
        context.log.info("all logins and permissions look fine")
        return
    for line in failures:
        context.log.warning(line)
    try:
        notifier = notify.notifier()
    except ConfigError as exc:
        context.log.warning(f"could not warn you on Telegram, it is incomplete: {exc}")
        return
    if notifier is None:
        context.log.warning("Telegram is not configured, so nobody was told")
        return
    sent = report_health(notify.services(), notifier, failures)
    context.log.info("warning sent" if sent else "already warned today")


@job
def token_health() -> None:
    token_health_op()
