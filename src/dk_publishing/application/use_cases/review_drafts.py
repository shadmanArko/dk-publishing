"""A person approves or rejects posts an agent submitted: in the Sheet's Calendar tab (ADR 0022) or
on the daily review page (ADR 0025). Both come through here, so there is one approval path.

Only variants made through the API that are still waiting (draft, or invalid after a refused
approval) react to a tick; a tick on anything else is stale and is ignored. Approving goes through
the same `approve` use case as everywhere else, so the content is validated and frozen as usual.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from dk_publishing.application.ports import SyncVariant
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases._common import move
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor, ActorKind
from dk_publishing.domain.sheet import CalendarDecision
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.sync import cancel_path

S = VariantStatus
SHEET_ACTOR = Actor(ActorKind.HUMAN, "sheet")
PAGE_ACTOR = Actor(ActorKind.HUMAN, "review-page")
WAITING = frozenset({S.DRAFT, S.PENDING_APPROVAL, S.INVALID})
MIN_LEAD = timedelta(minutes=1)  # the same rule as approving through the API


@dataclass
class ReviewReport:
    approved: int = 0
    rejected: int = 0
    problems: list[str] = field(default_factory=list)


def awaiting(sv: SyncVariant) -> bool:
    return sv.source == "agent" and sv.variant.status in WAITING


def review_drafts(
    services: Services,
    tenant_id: str,
    decisions: Sequence[CalendarDecision],
    variants: Sequence[SyncVariant],
    *,
    actor: Actor = SHEET_ACTOR,
    where: str = "the Sheet",
) -> ReviewReport:
    """Apply each decision to the matching agent post that is still waiting. `actor` and `where`
    only say who decided and where, in the post's history; the rules are the same everywhere."""
    report = ReviewReport()
    found = {(v.post_key, v.variant.platform, v.account_name or ""): v for v in variants}
    for d in decisions:
        sv = found.get((d.post_key, d.platform, d.account))
        label = f"{d.post_key}/{d.platform}"
        if sv is None or not awaiting(sv):
            continue  # a stale tick: the post already moved on
        if d.approve and d.reject:
            report.problems.append(f"{label}: tick approve or reject, not both. Nothing done.")
        elif d.reject:
            _reject(services, sv, report, label, actor, where)
        else:
            _approve(services, sv, report, label, actor)
    return report


def _reject(
    services: Services, sv: SyncVariant, report: ReviewReport, label: str, actor: Actor, where: str
) -> None:
    now = services.clock.now()
    with services.uow() as uow:
        moved = sv.variant
        for target in cancel_path(sv.variant.status):
            step = move(
                uow, moved, target, reason=f"rejected in {where}", at=now, next_step=None,
                actor=actor,
            )  # fmt: skip
            if step is None:
                report.problems.append(f"{label}: could not reject, it changed meanwhile.")
                return
            moved = step
        uow.commit()
    report.rejected += 1


def _approve(
    services: Services, sv: SyncVariant, report: ReviewReport, label: str, actor: Actor
) -> None:
    if sv.variant.status is S.INVALID:
        report.problems.append(
            f"{label}: this post was refused by the platform's rules and cannot be approved. "
            "Reject it; the agent must submit a corrected post."
        )
        return
    if sv.variant.publish_at < services.clock.now() + MIN_LEAD:
        report.problems.append(f"{label}: its slot has passed. Reject it; the agent must resubmit.")
        return
    content = sv.content
    if content is None:
        report.problems.append(f"{label}: the submitted content is missing; reject it.")
        return
    outcome, violations = approve(services, sv.variant.id, content, actor)
    if outcome is RunResult.APPROVED:
        report.approved += 1
    elif outcome is RunResult.INVALID:
        why = "; ".join(f"{v.field}: {v.message}" for v in violations)
        report.problems.append(f"{label}: not approved. {why}")
    # SKIPPED / LOST_RACE: somebody else decided first; the Calendar shows the result.
