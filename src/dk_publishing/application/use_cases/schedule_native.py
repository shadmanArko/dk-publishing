from __future__ import annotations

from dk_publishing.application.ports import NativeScheduler
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import (
    S,
    expire,
    move,
    own,
    planning_caps,
    settle_failure,
)
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.capabilities import for_delivery
from dk_publishing.domain.errors import IntegrityError
from dk_publishing.domain.planning import deadline, plan_next
from dk_publishing.domain.publishing import snapshot_for


def schedule_native_variant(
    services: Services, variant_id: str, expected_version: int
) -> RunResult:
    """Hand an approved post to the platform to publish at the slot, even if this system is off.

    A scheduling call cannot safely be repeated: if its outcome is uncertain the platform may now
    hold a scheduled copy we cannot see, so that case fails for a person to check (see
    `settle_failure`) instead of trying again.
    """
    now = services.clock.now()
    with services.uow() as uow:
        variant = uow.variants.get(variant_id)
        if (
            variant is None
            or variant.version != expected_version
            or variant.status is not S.APPROVED
        ):
            return RunResult.SKIPPED
        publisher = services.publishers.for_platform(variant.platform)
        if not isinstance(publisher, NativeScheduler):
            raise IntegrityError(f"{variant.platform} cannot schedule natively but was asked to")
        content = uow.variants.snapshot_of(variant_id)
        if content is None:
            raise IntegrityError(f"approved variant {variant_id} has no snapshot")
        caps = planning_caps(publisher, content)
        if now > deadline(variant.publish_at, caps):
            return expire(uow, variant, now)

        scheduling = move(
            uow, variant, S.SCHEDULING_NATIVE, reason="native scheduling started", at=now,
            next_step=None,
        )  # fmt: skip
        if scheduling is None:
            return RunResult.LOST_RACE
        attempt_id = uow.attempts.begin(
            tenant_id=variant.tenant_id,
            variant_id=variant_id,
            phase=Phase.SCHEDULE_NATIVE,
            idempotency_key=f"schedule_native:{variant_id}:{scheduling.version}",
            started_at=now,
        )
        uow.commit()

    window = caps.native_window
    if window is None or variant.publish_at - now < window[0]:
        # Delayed past the point where the platform would still accept it. Nothing was sent, so
        # fall back to publishing it ourselves at the slot, as the plan says.
        with services.uow() as uow:
            uow.attempts.finish(
                attempt_id, outcome=Outcome.OK, finished_at=now, error_code="released_to_direct"
            )
            step = plan_next(
                status=S.APPROVED,
                publish_at=variant.publish_at,
                caps=for_delivery(caps, "direct"),
                now=now,
            )
            own(
                move(
                    uow, scheduling, S.APPROVED, at=now, next_step=step,
                    reason="too close to the slot to schedule natively; publishing directly",
                )
            )  # fmt: skip
            uow.commit()
        return RunResult.RETRY_SCHEDULED

    try:
        files = (
            services.media_store.ensure_local(content.get("media") or [])
            if services.media_store is not None
            else []
        )
        handle = publisher.schedule(snapshot_for(scheduling, content), files, variant.publish_at)
        error = None
    except Exception as exc:
        handle, error = None, exc

    finished = services.clock.now()
    with services.uow() as uow:
        if error is not None or handle is None:
            result = settle_failure(
                uow,
                scheduling,
                error or RuntimeError("adapter returned no handle"),
                attempt_id=attempt_id,
                phase=Phase.SCHEDULE_NATIVE,
                resting=S.APPROVED,
                caps=caps,
                now=finished,
            )
        else:
            uow.attempts.finish(attempt_id, outcome=Outcome.OK, finished_at=finished)
            step = plan_next(
                status=S.SCHEDULED_NATIVE, publish_at=scheduling.publish_at, caps=caps, now=finished
            )
            own(
                move(
                    uow, scheduling, S.SCHEDULED_NATIVE, reason="scheduled on the platform",
                    at=finished, next_step=step, handle=handle,
                )
            )  # fmt: skip
            result = RunResult.SCHEDULED
        uow.commit()
    return result
