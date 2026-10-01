from __future__ import annotations

from datetime import timedelta

from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S, move, planning_caps
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


def recover_stale_preparing(
    services: Services, *, after: timedelta = STALE_AFTER, limit: int = 50
) -> list[str]:
    """A run that died mid-prepare leaves `preparing` behind. Preparing posts nothing and is safe
    to repeat, so the variant simply goes back to `approved` to be prepared again."""
    now = services.clock.now()
    recovered: list[str] = []
    with services.uow() as uow:
        for variant in uow.variants.stale(S.PREPARING, now - after, limit):
            publisher = services.publishers.for_platform(variant.platform)
            caps = planning_caps(publisher, uow.variants.snapshot_of(variant.id))
            step = plan_next(status=S.APPROVED, publish_at=variant.publish_at, caps=caps, now=now)
            moved = move(
                uow,
                variant,
                S.APPROVED,
                at=now,
                next_step=step,
                reason=f"no outcome recorded for {after}; preparing again",
            )
            if moved is not None:
                uow.attempts.finish_open(
                    variant.id, outcome=Outcome.UNKNOWN, finished_at=now, error_code="stale"
                )
                recovered.append(variant.id)
        uow.commit()
    return recovered


def fail_stale_scheduling(
    services: Services, *, after: timedelta = STALE_AFTER, limit: int = 50
) -> list[str]:
    """A run that died while handing a post to the platform may have left a scheduled copy this
    system cannot see. It is never retried: the variant fails and a person checks the platform."""
    now = services.clock.now()
    failed: list[str] = []
    with services.uow() as uow:
        for variant in uow.variants.stale(S.SCHEDULING_NATIVE, now - after, limit):
            moved = move(
                uow,
                variant,
                S.FAILED,
                at=now,
                next_step=None,
                reason=(
                    f"no outcome recorded for {after} while scheduling; the platform may hold a "
                    "scheduled copy. Check its scheduled posts, delete any copy by hand, then "
                    "edit this row to try again"
                ),
            )
            if moved is not None:
                uow.attempts.finish_open(
                    variant.id, outcome=Outcome.UNKNOWN, finished_at=now, error_code="stale"
                )
                failed.append(variant.id)
        uow.commit()
    return failed
