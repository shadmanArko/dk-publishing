from __future__ import annotations

from datetime import timedelta

from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S, move
from dk_publishing.domain.attempt import Outcome
from dk_publishing.domain.planning import plan_next

STALE_AFTER = timedelta(minutes=10)


def flag_stale_publishing(
    services: Services, *, after: timedelta = STALE_AFTER, limit: int = 50
) -> list[str]:
    """A run that died mid-publish leaves `publishing` behind. Mark it `unknown` so it gets
    reconciled. Returns the variant ids flagged. Never retries anything itself."""
    now = services.clock.now()
    flagged: list[str] = []
    with services.uow() as uow:
        for variant in uow.variants.stale(S.PUBLISHING, now - after, limit):
            caps = services.publishers.for_platform(variant.platform).capabilities
            step = plan_next(status=S.UNKNOWN, publish_at=variant.publish_at, caps=caps, now=now)
            moved = move(
                uow,
                variant,
                S.UNKNOWN,
                at=now,
                next_step=step,
                reason=f"no outcome recorded for {after}; the run probably died",
            )
            if moved is not None:
                uow.attempts.finish_open(
                    variant.id, outcome=Outcome.UNKNOWN, finished_at=now, error_code="stale"
                )
                flagged.append(variant.id)
        uow.commit()
    return flagged
