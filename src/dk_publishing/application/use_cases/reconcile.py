from __future__ import annotations

from dk_publishing.application.ports import DuplicateAttempt
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S, classify, move
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.errors import IntegrityError
from dk_publishing.domain.planning import plan_next
from dk_publishing.domain.publishing import snapshot_for


def reconcile_variant(services: Services, variant_id: str, expected_version: int) -> RunResult:
    """Settle an uncertain outcome by asking the platform, never by retrying the publish.

    Live -> published. Confirmed not live -> back to prepared, where the planner either
    republishes or expires it. If the platform cannot answer, the variant fails and a person
    checks by hand: guessing either way risks a duplicate or a silent loss.
    """
    now = services.clock.now()
    with services.uow() as uow:
        variant = uow.variants.get(variant_id)
        if (
            variant is None
            or variant.version != expected_version
            or variant.status is not S.UNKNOWN
        ):
            return RunResult.SKIPPED  # native-schedule verification arrives with native scheduling
        publisher = services.publishers.for_platform(variant.platform)
        content = uow.variants.snapshot_of(variant_id)
        if content is None:
            raise IntegrityError(f"variant {variant_id} has no snapshot to reconcile")
        handle = uow.variants.handle_of(variant_id)
        try:
            attempt_id = uow.attempts.begin(
                tenant_id=variant.tenant_id,
                variant_id=variant_id,
                phase=Phase.RECONCILE,
                idempotency_key=f"reconcile:{variant_id}:{variant.version}",
                started_at=now,
            )
        except DuplicateAttempt:
            return RunResult.LOST_RACE
        uow.commit()

    try:
        live = publisher.find_live(snapshot_for(variant, content), handle)
        error = None
    except Exception as exc:
        live, error = None, exc

    finished = services.clock.now()
    caps = publisher.capabilities
    with services.uow() as uow:
        if error is not None:
            outcome, code, _ = classify(error)
            uow.attempts.finish(
                attempt_id,
                outcome=outcome,
                finished_at=finished,
                error_code=code,
                response_excerpt=str(error) or None,
            )
            moved = move(
                uow,
                variant,
                S.FAILED,
                at=finished,
                next_step=None,
                reason=f"platform could not confirm the outcome; check by hand: {error}",
            )
            result = RunResult.FAILED
        elif live is not None:
            uow.attempts.finish(attempt_id, outcome=Outcome.OK, finished_at=finished)
            moved = move(
                uow,
                variant,
                S.PUBLISHED,
                reason="reconciled: the post is live",
                at=finished,
                next_step=None,
                live=live,
            )
            result = RunResult.PUBLISHED
        else:
            uow.attempts.finish(attempt_id, outcome=Outcome.OK, finished_at=finished)
            step = plan_next(
                status=S.PREPARED, publish_at=variant.publish_at, caps=caps, now=finished
            )
            moved = move(
                uow,
                variant,
                S.PREPARED,
                reason="reconciled: confirmed not live",
                at=finished,
                next_step=step,
            )
            result = RunResult.NOT_LIVE
        uow.commit()
    return result if moved is not None else RunResult.LOST_RACE
