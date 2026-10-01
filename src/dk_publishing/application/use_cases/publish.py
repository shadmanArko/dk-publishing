from __future__ import annotations

from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S, expire, move, own, settle_failure
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.errors import IntegrityError
from dk_publishing.domain.planning import deadline
from dk_publishing.domain.snapshot import snapshot_hash


def publish_variant(services: Services, variant_id: str, expected_version: int) -> RunResult:
    """The one place a post goes live, and the one place a duplicate could be made.

    Order matters and is the whole guarantee:
      1. compare-and-set into `publishing` and write the intent log, committed together;
      2. only then make the call, with no transaction open;
      3. record the outcome. If the process dies between 2 and 3 the row stays `publishing`,
         housekeeping turns it into `unknown`, and reconciliation asks the platform.
    A second run holding the same version loses step 1 and never reaches the platform.
    """
    now = services.clock.now()
    with services.uow() as uow:
        variant = uow.variants.get(variant_id)
        if (
            variant is None
            or variant.version != expected_version
            or variant.status is not S.PREPARED
        ):
            return RunResult.SKIPPED
        publisher = services.publishers.for_platform(variant.platform)
        caps = publisher.capabilities
        if now > deadline(variant.publish_at, caps):
            return expire(uow, variant, now)

        content = uow.variants.snapshot_of(variant_id)
        handle = uow.variants.handle_of(variant_id)
        # Fidelity: what is sent must be exactly what was approved.
        if content is None or snapshot_hash(content) != variant.snapshot_hash:
            raise IntegrityError(f"snapshot of {variant_id} no longer matches its approved hash")
        if handle is None:
            raise IntegrityError(f"prepared variant {variant_id} has no handle")

        publishing = move(
            uow, variant, S.PUBLISHING, reason="publish started", at=now, next_step=None
        )
        if publishing is None:
            return RunResult.LOST_RACE
        attempt_id = uow.attempts.begin(
            tenant_id=variant.tenant_id,
            variant_id=variant_id,
            phase=Phase.PUBLISH,
            idempotency_key=f"publish:{variant_id}:{publishing.version}",
            started_at=now,
        )
        uow.commit()

    # No transaction is open here. A crash from this point leaves `publishing` behind on purpose.
    try:
        live = publisher.publish(handle)
        error = None
    except Exception as exc:
        live, error = None, exc

    finished = services.clock.now()
    with services.uow() as uow:
        if error is not None or live is None:
            result = settle_failure(
                uow,
                publishing,
                error or RuntimeError("adapter returned no post"),
                attempt_id=attempt_id,
                phase=Phase.PUBLISH,
                resting=S.PREPARED,
                caps=caps,
                now=finished,
            )
        else:
            uow.attempts.finish(attempt_id, outcome=Outcome.OK, finished_at=finished)
            own(
                move(
                    uow,
                    publishing,
                    S.PUBLISHED,
                    reason="published",
                    at=finished,
                    next_step=None,
                    live=live,
                )
            )
            result = RunResult.PUBLISHED
        uow.commit()
    return result
