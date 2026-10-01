from __future__ import annotations

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
from dk_publishing.domain.errors import IntegrityError
from dk_publishing.domain.planning import deadline, plan_next
from dk_publishing.domain.publishing import snapshot_for


def prepare_variant(services: Services, variant_id: str, expected_version: int) -> RunResult:
    """Do the slow, harmless work ahead of the slot: uploads, containers. Posts nothing."""
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
        caps = publisher.capabilities
        if now > deadline(variant.publish_at, caps):
            return expire(uow, variant, now)

        preparing = move(
            uow, variant, S.PREPARING, reason="prepare started", at=now, next_step=None
        )
        if preparing is None:
            return RunResult.LOST_RACE
        content = uow.variants.snapshot_of(variant_id)
        if content is None:
            raise IntegrityError(f"approved variant {variant_id} has no snapshot")
        attempt_id = uow.attempts.begin(
            tenant_id=variant.tenant_id,
            variant_id=variant_id,
            phase=Phase.PREPARE,
            idempotency_key=f"prepare:{variant_id}:{preparing.version}",
            started_at=now,
        )
        uow.commit()  # ownership and intent are durable before the slow call; no lock is held

    try:
        files = (
            services.media_store.ensure_local(content.get("media") or [])
            if services.media_store is not None
            else []
        )
        handle = publisher.prepare(snapshot_for(preparing, content), files)
        error = None
    except Exception as exc:
        handle, error = None, exc

    finished = services.clock.now()
    with services.uow() as uow:
        if error is not None or handle is None:
            result = settle_failure(
                uow,
                preparing,
                error or RuntimeError("adapter returned no handle"),
                attempt_id=attempt_id,
                phase=Phase.PREPARE,
                resting=S.APPROVED,
                caps=planning_caps(publisher, content),
                now=finished,
            )
        else:
            uow.attempts.finish(attempt_id, outcome=Outcome.OK, finished_at=finished)
            step = plan_next(
                status=S.PREPARED, publish_at=preparing.publish_at, caps=caps, now=finished
            )
            own(
                move(
                    uow,
                    preparing,
                    S.PREPARED,
                    reason="prepared",
                    at=finished,
                    next_step=step,
                    handle=handle,
                )
            )
            result = RunResult.PREPARED
        uow.commit()
    return result
