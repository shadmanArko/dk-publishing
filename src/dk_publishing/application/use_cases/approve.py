from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import S, move
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor
from dk_publishing.domain.planning import plan_next
from dk_publishing.domain.publishing import Violation, snapshot_for
from dk_publishing.domain.snapshot import snapshot_hash


def approve(
    services: Services, variant_id: str, content: Mapping[str, Any], actor: Actor
) -> tuple[RunResult, list[Violation]]:
    """Validate the content, freeze it as the snapshot, and move a draft to approved."""
    now = services.clock.now()
    with services.uow() as uow:
        variant = uow.variants.get(variant_id)
        if variant is None or variant.status not in (S.DRAFT, S.PENDING_APPROVAL):
            return RunResult.SKIPPED, []
        publisher = services.publishers.for_platform(variant.platform)

        violations = publisher.validate(snapshot_for(variant, content))
        if violations:
            reason = "; ".join(f"{v.field}: {v.message}" for v in violations)
            if variant.status is S.DRAFT:
                move(uow, variant, S.INVALID, reason=reason, at=now, next_step=None, actor=actor)
                uow.commit()
            return RunResult.INVALID, violations

        step = plan_next(
            status=S.APPROVED,
            publish_at=variant.publish_at,
            caps=publisher.capabilities,
            now=now,
            media_ready=True,  # the media pipeline arrives in a later slice
        )
        approved = move(
            uow,
            variant,
            S.APPROVED,
            reason="approved",
            at=now,
            next_step=step,
            actor=actor,
            snapshot_hash=snapshot_hash(content),
            snapshot=content,
        )
        uow.commit()
        return (RunResult.APPROVED if approved else RunResult.LOST_RACE), []
