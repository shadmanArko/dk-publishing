"""One small job per action. An op is three lines: build, call, log. No business logic here."""

from collections.abc import Callable

from dagster import Config, Failure, JobDefinition, OpExecutionContext, job, op

from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.dispatch import expire_variant
from dk_publishing.application.use_cases.housekeeping import (
    flag_stale_publishing,
    recover_stale_preparing,
)
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.application.use_cases.sync_sheet import sync_sheet
from dk_publishing.entrypoints.dagster_defs.resources import ServicesResource, SheetSyncResource


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
publish_variant_job = _action_job("publish_variant", publish_variant)
reconcile_variant_job = _action_job("reconcile_variant", reconcile_variant)
expire_variant_job = _action_job("expire_variant", expire_variant)


@op
def housekeeping_op(context: OpExecutionContext, services: ServicesResource) -> None:
    svc = services.services()
    uncertain, reprepared = flag_stale_publishing(svc), recover_stale_preparing(svc)
    if uncertain:
        context.log.warning(f"runs died mid-publish, now being reconciled: {uncertain}")
    context.log.info(f"housekeeping: {len(uncertain)} uncertain, {len(reprepared)} re-prepared")


@job
def housekeeping() -> None:
    housekeeping_op()


@op
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
        raise Failure(description=report.halted)  # a person must look before anything is cancelled
    if report.errors:
        raise Failure(description="; ".join(report.errors))


@job(name="sync_sheet")
def sync_sheet_job() -> None:
    sync_sheet_op()
