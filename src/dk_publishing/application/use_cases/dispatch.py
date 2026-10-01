from __future__ import annotations

from dk_publishing.application.ports import DueAction
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S, expire
from dk_publishing.application.use_cases.housekeeping import (
    flag_stale_publishing,
    recover_stale_preparing,
)
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.planning import Action


def expire_variant(services: Services, variant_id: str, expected_version: int) -> RunResult:
    now = services.clock.now()
    with services.uow() as uow:
        variant = uow.variants.get(variant_id)
        if variant is None or variant.version != expected_version:
            return RunResult.SKIPPED
        if variant.status not in (S.APPROVED, S.PREPARED):
            return RunResult.SKIPPED
        return expire(uow, variant, now)


def run_action(services: Services, due: DueAction) -> RunResult:
    """What one Dagster run does: call the use case for a due action, keyed by its version."""
    match due.action:
        case Action.PREPARE:
            return prepare_variant(services, due.variant_id, due.version)
        case Action.PUBLISH:
            return publish_variant(services, due.variant_id, due.version)
        case Action.RECONCILE:
            return reconcile_variant(services, due.variant_id, due.version)
        case Action.EXPIRE:
            return expire_variant(services, due.variant_id, due.version)
        case Action.FETCH_MEDIA | Action.SCHEDULE_NATIVE:
            raise NotImplementedError(f"{due.action} arrives with the media and native slices")


def run_due(services: Services, limit: int = 50) -> list[tuple[DueAction, RunResult]]:
    """One tick of the scheduler: flag dead runs, then run everything that is due."""
    flag_stale_publishing(services)
    recover_stale_preparing(services)
    with services.uow() as uow:
        due = uow.variants.due(services.clock.now(), limit)
    return [(item, run_action(services, item)) for item in due]
