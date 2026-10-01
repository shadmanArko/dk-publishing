"""Helpers shared by the use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from dk_publishing.application.ports import Handle, NativeScheduler, Publisher, UnitOfWork
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain import errors
from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.capabilities import Capabilities, for_delivery
from dk_publishing.domain.lifecycle import transition
from dk_publishing.domain.model import Actor, ActorKind, Variant
from dk_publishing.domain.planning import NextStep, deadline, plan_next, retry_at
from dk_publishing.domain.publishing import LivePost, Violation
from dk_publishing.domain.status import VariantStatus

SCHEDULER = Actor(ActorKind.SYSTEM, "scheduler")
S = VariantStatus


DELIVERIES = ("direct", "native")


def delivery_of(content: Mapping[str, Any] | None) -> str:
    return str((content or {}).get("delivery") or "direct")


def planning_caps(publisher: Publisher, content: Mapping[str, Any] | None) -> Capabilities:
    """The capabilities to plan this variant with, honouring its `delivery` choice."""
    return for_delivery(publisher.capabilities, delivery_of(content))


def delivery_problems(
    publisher: Publisher, content: Mapping[str, Any], publish_at: datetime, now: datetime
) -> list[Violation]:
    """Plain-language reasons the chosen delivery cannot work, checked before approval."""
    delivery = delivery_of(content)
    if delivery not in DELIVERIES:
        return [Violation("delivery", f"delivery must be one of: {', '.join(DELIVERIES)}.")]
    if delivery != "native":
        return []
    window = publisher.capabilities.native_window
    if not isinstance(publisher, NativeScheduler) or window is None:
        return [
            Violation("delivery", "This platform cannot hold scheduled posts. Use direct instead.")
        ]
    shortest = window[0]
    if publish_at - now < shortest:
        minutes = int(shortest.total_seconds() // 60)
        return [
            Violation(
                "delivery",
                f"Native scheduling needs the slot at least {minutes} minutes from now. "
                "Move the slot later, or use direct.",
            )
        ]
    return []


def move(
    uow: UnitOfWork,
    variant: Variant,
    to: VariantStatus,
    *,
    reason: str,
    at: datetime,
    next_step: NextStep | None,
    actor: Actor = SCHEDULER,
    snapshot_hash: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
    handle: Handle | None = None,
    live: LivePost | None = None,
    publish_at: datetime | None = None,
    source_hash: str | None = None,
) -> Variant | None:
    """Transition and persist in one compare-and-set. None means another run got there first."""
    moved, event = transition(
        variant, to, actor=actor, reason=reason, at=at, snapshot_hash=snapshot_hash
    )
    if publish_at is not None:
        moved = replace(moved, publish_at=publish_at)
    applied = uow.variants.apply(
        variant,
        moved,
        event,
        next_step,
        snapshot=snapshot,
        handle=handle,
        live=live,
        source_hash=source_hash,
    )
    return moved if applied else None


def own(moved: Variant | None) -> Variant:
    """For a variant in an in-flight state, which no other run may change."""
    if moved is None:
        raise RuntimeError("lost a variant that this run exclusively owned")
    return moved


def classify(error: Exception) -> tuple[Outcome, str, timedelta | None]:
    """Map what an adapter raised to (outcome, code, retry_after).

    Anything unexpected is treated as UnknownOutcome: after a call may have been sent, assuming
    the worst is the only choice that cannot create a duplicate.
    """
    if isinstance(error, errors.RateLimited):
        return Outcome.RATE_LIMITED, "rate_limited", error.retry_after
    if isinstance(error, errors.Retryable):
        return Outcome.RETRYABLE, "retryable", None
    if isinstance(error, errors.AuthFailed):
        return Outcome.AUTH_FAILED, "auth_failed", None
    if isinstance(error, errors.Rejected):
        return Outcome.REJECTED, "rejected", None
    if isinstance(error, errors.UnknownOutcome):
        return Outcome.UNKNOWN, "unknown_outcome", None
    return Outcome.UNKNOWN, type(error).__name__, None


def settle_failure(
    uow: UnitOfWork,
    in_flight: Variant,
    error: Exception,
    *,
    attempt_id: str,
    phase: Phase,
    resting: VariantStatus,
    caps: Capabilities,
    now: datetime,
) -> RunResult:
    """Record a failed call and move the in-flight variant to where that failure leads.

    `resting` is the state a retry returns to: APPROVED after a failed prepare, PREPARED after a
    failed publish. Retries are domain state: the delay is written into next_action_at, and
    nothing is ever scheduled past the deadline.
    """
    outcome, code, retry_after = classify(error)
    uow.attempts.finish(
        attempt_id,
        outcome=outcome,
        finished_at=now,
        error_code=code,
        response_excerpt=str(error) or None,
    )

    if outcome is Outcome.AUTH_FAILED:
        own(
            move(
                uow,
                in_flight,
                resting,
                reason="authorisation failed; reconnect the account",
                at=now,
                next_step=None,
            )
        )
        return RunResult.PARKED

    if outcome is Outcome.REJECTED:
        own(
            move(
                uow,
                in_flight,
                S.FAILED,
                reason=f"platform rejected it: {error}",
                at=now,
                next_step=None,
            )
        )
        return RunResult.FAILED

    if outcome is Outcome.UNKNOWN and phase is Phase.SCHEDULE_NATIVE:
        # The platform may now be holding a scheduled copy this system cannot see. Scheduling
        # again could publish the post twice, so a person checks instead.
        own(
            move(
                uow, in_flight, S.FAILED, at=now, next_step=None,
                reason=(
                    "It is uncertain whether the platform scheduled this post. Check its "
                    "scheduled posts and delete any copy by hand, then edit this row to try "
                    f"again: {error}"
                ),
            )
        )  # fmt: skip
        return RunResult.FAILED

    if outcome is Outcome.UNKNOWN and phase is Phase.PUBLISH:
        # The request may have reached the platform. Never retry: reconcile first.
        step = plan_next(status=S.UNKNOWN, publish_at=in_flight.publish_at, caps=caps, now=now)
        own(
            move(
                uow,
                in_flight,
                S.UNKNOWN,
                reason=f"outcome uncertain: {code}",
                at=now,
                next_step=step,
            )
        )
        return RunResult.FLAGGED_UNKNOWN

    # Retryable, rate limited, or an uncertain *prepare* (it posts nothing, so repeating is safe).
    failures = uow.attempts.failures(in_flight.id, phase)
    when = retry_at(
        now=now,
        limit=deadline(in_flight.publish_at, caps),
        failures=failures,
        retry_after=retry_after,
    )
    if when is None:
        reason = f"gave up after {failures} failed tries or reaching the deadline: {error}"
        own(move(uow, in_flight, S.FAILED, reason=reason, at=now, next_step=None))
        return RunResult.FAILED
    step = plan_next(
        status=resting, publish_at=in_flight.publish_at, caps=caps, now=now, not_before=when
    )
    own(move(uow, in_flight, resting, reason=f"will retry: {error}", at=now, next_step=step))
    return RunResult.RETRY_SCHEDULED


def expire(uow: UnitOfWork, variant: Variant, now: datetime) -> RunResult:
    moved = move(
        uow, variant, S.EXPIRED, reason="past max lateness; not published", at=now, next_step=None
    )
    uow.commit()
    return RunResult.EXPIRED if moved else RunResult.LOST_RACE
