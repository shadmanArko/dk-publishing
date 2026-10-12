"""The owner's daily review: what the page shows for a day, and what its buttons do (ADR 0025).

Reading is a view over the same rows the API and the Calendar use. Deciding is not a new path at
all: every button ends in `review_drafts`, the function the Sheet's tick boxes already use, so a
post is validated, frozen, hashed and withdrawn exactly as it is everywhere else. The page can
approve only what an agent made (`source = 'agent'`) and only what is still waiting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from dk_publishing.application.ports import MediaPreview, SyncVariant
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.api_posts import STATE
from dk_publishing.application.use_cases.review_drafts import (
    MIN_LEAD,
    PAGE_ACTOR,
    awaiting,
    review_drafts,
)
from dk_publishing.domain.model import Actor
from dk_publishing.domain.sheet import CalendarDecision
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.timezones import utc_to_berlin

S = VariantStatus
VIDEO_TYPES = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv")
GENERAL_FIELDS = ("format", "caption", "media")  # everything else a variant carries is an option
MAX_DECISIONS = 100  # one tap never decides more than this
APPROVABLE = (S.DRAFT, S.PENDING_APPROVAL)


@dataclass(frozen=True, slots=True)
class MediaShown:
    """One file of an idea, shown once however many platforms use it."""

    name: str
    kind: str  # "video" | "image"
    url: str | None  # None: nothing to play (no link settings, or the file was removed)
    platforms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VariantCard:
    variant_id: str
    platform: str
    account: str
    format: str
    caption: str
    slot: datetime  # aware UTC; the page shows it in Berlin time
    state: str  # the API's words: awaiting_approval, scheduled, published, ...
    options: tuple[tuple[str, str], ...]
    media_names: tuple[str, ...]
    can_approve: bool
    can_reject: bool
    note: str  # why a waiting post cannot be approved, or what went wrong


@dataclass(frozen=True, slots=True)
class IdeaCard:
    post_key: str
    created_by: str
    variants: tuple[VariantCard, ...]
    media: tuple[MediaShown, ...]
    other_days: tuple[date, ...]  # the same idea has variants on these other Berlin days

    @property
    def approvable(self) -> tuple[str, ...]:
        return tuple(v.variant_id for v in self.variants if v.can_approve)

    @property
    def rejectable(self) -> tuple[str, ...]:
        return tuple(v.variant_id for v in self.variants if v.can_reject)


@dataclass(frozen=True, slots=True)
class DayReview:
    day: date
    ideas: tuple[IdeaCard, ...]

    @property
    def approvable(self) -> tuple[str, ...]:
        return tuple(i for idea in self.ideas for i in idea.approvable)


@dataclass(frozen=True, slots=True)
class Decided:
    approved: int
    rejected: int
    already: int  # decided meanwhile (elsewhere, or by an earlier tap): nothing was done
    problems: tuple[str, ...]


def berlin_day(moment: datetime) -> date:
    return utc_to_berlin(moment).date()


def _kind(name: str) -> str:
    return "video" if name.lower().endswith(VIDEO_TYPES) else "image"


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list | tuple):
        return ", ".join(_text(v) for v in value)
    return str(value)


def _can_approve(sv: SyncVariant, now: datetime) -> bool:
    return (
        sv.source == "agent"
        and sv.variant.status in APPROVABLE
        and sv.content is not None
        and sv.variant.publish_at >= now + MIN_LEAD
    )


def _note(sv: SyncVariant, now: datetime) -> str:
    status = sv.variant.status
    if awaiting(sv):
        if status is S.INVALID:
            return f"The platform's rules refused it: {sv.last_reason or 'see the Calendar'}."
        if sv.variant.publish_at < now + MIN_LEAD:
            return "Its slot has passed. Reject it; the agent must resubmit."
        if sv.content is None:
            return "Its content is missing. Reject it."
    if status in (S.FAILED, S.EXPIRED, S.UNKNOWN, S.CANCELLED) and sv.last_reason:
        return sv.last_reason
    return ""


def _card(sv: SyncVariant, now: datetime) -> VariantCard:
    content: Mapping[str, Any] = sv.content or {}
    media = content.get("media") or []
    return VariantCard(
        variant_id=sv.variant.id,
        platform=sv.variant.platform,
        account=sv.account_name or "",
        format=str(content.get("format") or ""),
        caption=str(content.get("caption") or ""),
        slot=sv.variant.publish_at,
        state=STATE[sv.variant.status],
        options=tuple(
            (str(k), _text(v))
            for k, v in content.items()
            if k not in GENERAL_FIELDS and v not in (None, "", [], {})
        ),
        media_names=tuple(str(m.get("name", "")) for m in media if isinstance(m, dict)),
        can_approve=_can_approve(sv, now),
        can_reject=awaiting(sv),
        note=_note(sv, now),
    )


def _media(members: Sequence[SyncVariant], preview: MediaPreview | None) -> tuple[MediaShown, ...]:
    """Each distinct file of the idea once, with the platforms that use it."""
    found: dict[tuple[str, str], tuple[Mapping[str, Any], list[str]]] = {}
    for sv in members:
        for item in (sv.content or {}).get("media") or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("sha256") or item.get("media_id") or ""), str(item.get("name")))
            used = found.setdefault(key, (item, []))[1]
            if sv.variant.platform not in used:
                used.append(sv.variant.platform)
    shown = []
    for item, platforms in found.values():
        name = str(item.get("name") or "file")
        url = preview.url_for(item) if preview is not None else None
        shown.append(MediaShown(name, _kind(name), url, tuple(platforms)))
    return tuple(shown)


def load_day(
    services: Services, tenant_id: str, day: date, preview: MediaPreview | None = None
) -> DayReview:
    """Every agent post with a slot on this Berlin day, grouped by idea (post_key)."""
    now = services.clock.now()
    with services.uow() as uow:
        every = uow.sync.variants(tenant_id)
    agent = [v for v in every if v.source == "agent"]
    ideas: dict[str, list[SyncVariant]] = {}
    for sv in agent:
        if berlin_day(sv.variant.publish_at) == day:
            ideas.setdefault(sv.post_key, []).append(sv)
    cards = []
    for key, members in ideas.items():
        members.sort(key=lambda v: (v.variant.publish_at, v.variant.platform))
        elsewhere = sorted(
            {
                berlin_day(v.variant.publish_at)
                for v in agent
                if v.post_key == key and berlin_day(v.variant.publish_at) != day
            }
        )
        cards.append(
            IdeaCard(
                post_key=key,
                created_by=members[0].created_by or "",
                variants=tuple(_card(v, now) for v in members),
                media=_media(members, preview),
                other_days=tuple(elsewhere),
            )
        )
    cards.sort(key=lambda c: (c.variants[0].slot, c.post_key))
    return DayReview(day, tuple(cards))


def waiting_on(services: Services, tenant_id: str, day: date) -> int:
    """How many agent posts on this Berlin day wait for the owner and can still be approved."""
    now = services.clock.now()
    with services.uow() as uow:
        every = uow.sync.variants(tenant_id)
    return sum(1 for v in every if _can_approve(v, now) and berlin_day(v.variant.publish_at) == day)


def decide(
    services: Services,
    tenant_id: str,
    variant_ids: Sequence[str],
    *,
    approve: bool,
    actor: Actor = PAGE_ACTOR,
) -> Decided:
    """Approve or reject exactly these variants (what the owner saw), through the same function
    the Sheet's tick boxes use. A variant that is no longer waiting is counted, not touched."""
    wanted = set(variant_ids[:MAX_DECISIONS])
    with services.uow() as uow:
        every = uow.sync.variants(tenant_id)
    chosen = [v for v in every if v.variant.id in wanted and v.source == "agent"]
    decisions = [
        CalendarDecision(
            0, v.post_key, v.variant.platform, v.account_name or "", approve, not approve
        )
        for v in chosen
    ]
    report = review_drafts(
        services, tenant_id, decisions, every, actor=actor, where="the review page"
    )
    done = report.approved + report.rejected
    already = max(0, len(wanted) - done - len(report.problems))
    return Decided(report.approved, report.rejected, already, tuple(report.problems))
