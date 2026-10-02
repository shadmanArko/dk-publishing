"""Read the Sheet, make Postgres agree with it, and write the results back.

The Sheet is an input, never the record: Postgres holds what was approved. Rows are matched to
variants by (post_key, platform, account), so sorting or filtering the Sheet is always safe.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dk_publishing.application.ports import AccountInfo, SyncVariant
from dk_publishing.application.services import Services, SyncServices
from dk_publishing.application.use_cases._common import delivery_problems, move
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.native import withdraw_native
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor, ActorKind, Variant
from dk_publishing.domain.publishing import VariantSnapshot, Violation
from dk_publishing.domain.sheet import (
    CalendarEntry,
    PlatformRow,
    PostRow,
    PostStatus,
    RowStatus,
    StatusPlan,
)
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.sync import (
    SCHEDULED,
    Step,
    cancel_path,
    changed_text,
    content_for,
    effective_slot,
    join_problems,
    plan_steps,
    post_summary,
    resolve_media,
    resolve_slot,
    row_status,
    source_hash,
)

S = VariantStatus
SHEET_ACTOR = Actor(ActorKind.HUMAN, "sheet")
CALENDAR_STATES = frozenset(
    {
        S.APPROVED,
        S.PREPARING,
        S.PREPARED,
        S.SCHEDULING_NATIVE,
        S.SCHEDULED_NATIVE,
        S.PUBLISHING,
        S.UNKNOWN,
        S.PUBLISHED,
    }
)
REMOVED = "removed"  # source_hash of a variant whose row was deleted
Key = tuple[str, str, str]  # (post_key, platform, account_id)


@dataclass
class SyncReport:
    created: int = 0
    approved: int = 0
    marked_invalid: int = 0
    withdrawn: int = 0  # approvals withdrawn because the row was edited
    cancelled: int = 0
    left_live: int = 0
    busy: int = 0
    cells_written: int = 0
    problems: list[str] = field(default_factory=list)  # about the Sheet itself
    errors: list[str] = field(default_factory=list)  # a variant that could not be updated
    halted: str | None = None

    @property
    def changed(self) -> bool:
        return bool(
            self.created or self.approved or self.marked_invalid or self.withdrawn or self.cancelled
        )


@dataclass
class _Desired:
    row: PlatformRow
    post: PostRow | None
    account: AccountInfo | None
    publish_at: datetime | None
    content: dict[str, Any] | None
    source_hash: str
    problems: list[Violation]

    @property
    def key(self) -> Key | None:
        if self.account is None or not self.row.post_key:
            return None
        return (self.row.post_key, self.row.platform, self.account.id)


def sync_sheet(sync: SyncServices, *, allow_cancellations: bool = False) -> SyncReport:
    core, tenant = sync.core, sync.tenant_id
    now = core.clock.now()
    snapshot = sync.sheet.read()
    files = sync.media.list_files()
    report = SyncReport(problems=list(snapshot.problems))

    unique_posts, duplicate_keys = _index_posts(snapshot.posts)
    with core.uow() as uow:
        accounts = uow.sync.accounts(tenant)
        for post in unique_posts.values():
            uow.sync.upsert_post(tenant, post.post_key, post.title)
        uow.commit()
        existing = uow.sync.variants(tenant)

    desired = [
        _desire(row, unique_posts, duplicate_keys, accounts, files, core, now, tenant)
        for row in snapshot.rows
    ]
    ambiguous = _ambiguous(desired)
    by_key = {d.key: d for d in desired if d.key and d.key not in ambiguous}
    existing_by_key = {(v.post_key, v.variant.platform, v.variant.account_id): v for v in existing}

    # 1. what would be cancelled, before anything is touched
    to_cancel: list[tuple[SyncVariant, _Desired | None]] = []
    for key, sv in existing_by_key.items():
        if key in ambiguous:
            continue
        d = by_key.get(key)
        wanted_off = d is None or not d.row.enabled
        if wanted_off and S.CANCELLED in cancel_path(sv.variant.status) and _cancellable(sv):
            to_cancel.append((sv, d))
    scheduled = [sv for sv, _ in to_cancel if sv.variant.status in SCHEDULED]
    if len(scheduled) > sync.max_cancellations and not allow_cancellations:
        names = ", ".join(f"{sv.post_key}/{sv.variant.platform}" for sv in scheduled[:8])
        report.halted = (
            f"This sync would cancel {len(scheduled)} scheduled posts (more than "
            f"{sync.max_cancellations}), which looks like rows were deleted by accident. "
            f"Nothing was changed. Affected: {names}. Restore the rows, or confirm with "
            "`dk sync --allow-cancellations`."
        )
        return report

    # 2. apply
    for sv, d in to_cancel:
        _guarded(report, f"{sv.post_key}/{sv.variant.platform}", _cancel, core, sv, d, now, report)
    for key, d in by_key.items():
        _guarded(
            report, f"{key[0]}/{key[1]}", _reconcile, sync, d, existing_by_key.get(key), now, report
        )

    # 3. tell the person what happened
    with core.uow() as uow:
        fresh = uow.sync.variants(tenant)
        accounts = uow.sync.accounts(tenant)
        if report.changed:
            uow.sync.save_snapshots(tenant, str(uuid.uuid4()), snapshot.raw)
            uow.commit()
    plan = _status_plan(sync, snapshot.posts, desired, ambiguous, fresh, accounts, now)
    report.cells_written = sync.sheet.write(plan)
    return report


# --- reading the rows -------------------------------------------------------------------------


def _index_posts(posts: list[PostRow]) -> tuple[dict[str, PostRow], set[str]]:
    counts = Counter(p.post_key for p in posts if p.post_key)
    duplicates = {key for key, n in counts.items() if n > 1}
    unique = {p.post_key: p for p in posts if p.post_key and p.post_key not in duplicates}
    return unique, duplicates


def _desire(
    row: PlatformRow,
    posts: dict[str, PostRow],
    duplicate_keys: set[str],
    accounts: list[AccountInfo],
    files: list[Any],
    core: Services,
    now: datetime,
    tenant: str,
) -> _Desired:
    post = posts.get(row.post_key)
    problems = list(row.problems)
    if row.post_key in duplicate_keys:
        problems.append(
            Violation("post_key", f"post_key '{row.post_key}' appears more than once on Posts.")
        )
    elif row.post_key and post is None:
        problems.append(
            Violation("post_key", f"post_key '{row.post_key}' is not on the Posts tab.")
        )
    if post:
        problems.extend(post.problems)

    try:
        publisher = core.publishers.for_platform(row.platform)
    except KeyError:  # not registered: the platform is switched off in platforms.yaml
        publisher = None
        problems.append(Violation("platform", "This platform is switched off."))

    name = row.account.strip()
    account = next(
        (
            a
            for a in accounts
            if a.platform == row.platform
            and a.status == "active"
            and (a.display_name or "").strip().lower() == name.lower()
        ),
        None,
    )
    if publisher is not None and name and account is None:
        problems.append(
            Violation(
                "account",
                f"Account '{name}' is not connected for this platform. Pick one from the dropdown.",
            )
        )

    media, media_problems = resolve_media(post.media if post else (), files)
    problems.extend(media_problems)
    publish_at, slot_problems = resolve_slot(effective_slot(post, row), now)
    problems.extend(slot_problems)
    content = content_for(post, row, media) if post else None

    if content is not None and account is not None and publisher is not None:
        probe = VariantSnapshot(
            "pending", tenant, row.platform, account.id, publish_at or now, content
        )
        problems.extend(publisher.validate(probe))
        if publish_at is not None:
            problems.extend(delivery_problems(publisher, content, publish_at, now))

    if not row.enabled:
        problems = []  # a switched-off row is not asked to be valid
    return _Desired(
        row, post, account, publish_at, content, source_hash(post, row, media), problems
    )


def _ambiguous(desired: list[_Desired]) -> set[Key]:
    counts = Counter(d.key for d in desired if d.key)
    repeated = {key for key, n in counts.items() if n > 1}
    for d in desired:
        if d.key in repeated:
            d.problems.append(
                Violation(
                    "post_key", "This post is listed twice for the same platform and account."
                )
            )
    return repeated


# --- applying ---------------------------------------------------------------------------------


def _cancellable(sv: SyncVariant) -> bool:
    return sv.variant.status in {S.DRAFT, S.INVALID, *SCHEDULED}


def _guarded(report: SyncReport, label: str, fn: Any, *args: Any) -> None:
    """One bad variant must not stop the rest; the sync is idempotent and will retry it."""
    try:
        fn(*args)
    except Exception as exc:
        report.errors.append(f"{label}: {type(exc).__name__}: {exc}")


def _cancel(
    core: Services, sv: SyncVariant, d: _Desired | None, now: datetime, report: SyncReport
) -> None:
    # Record what the row looked like when it was cancelled (or that it was removed), so that
    # putting the row back, or re-ticking `enabled`, counts as an edit and revives the post.
    marker = d.source_hash if d else REMOVED
    # A post the platform is holding must be taken back first, or it would still go out.
    if not withdraw_native(core, sv.variant, now):
        report.busy += 1  # past its slot: the platform may have published it; reconcile decides
        return
    with core.uow() as uow:
        variant: Variant | None = sv.variant
        for target in cancel_path(sv.variant.status):
            assert variant is not None
            variant = move(
                uow,
                variant,
                target,
                reason="row removed or switched off in the Sheet",
                at=now,
                next_step=None,
                actor=SHEET_ACTOR,
                source_hash=marker,
            )
            if variant is None:
                return  # lost the race to the scheduler; the next sync will look again
        uow.commit()
    report.cancelled += 1


def _reconcile(
    sync: SyncServices, d: _Desired, sv: SyncVariant | None, now: datetime, report: SyncReport
) -> None:
    core = sync.core
    changed = sv is not None and sv.source_hash != d.source_hash
    steps = plan_steps(
        sv.variant.status if sv else None,
        changed=changed,
        enabled=d.row.enabled,
        ready=bool(d.post and d.post.ready),
        valid=not d.problems,
    )
    variant = sv.variant if sv else None
    reason = join_problems(d.problems)

    for step in steps:
        if step is Step.CREATE:
            if d.publish_at is None or d.post is None or d.account is None:
                return  # no usable slot yet: nothing to create, the row just shows why
            variant = _create(sync, d)
            report.created += 1
        elif step is Step.UPDATE_INPUTS:
            assert variant is not None
            with core.uow() as uow:
                if not uow.sync.update_inputs(
                    variant.id,
                    publish_at=d.publish_at or variant.publish_at,
                    source_hash=d.source_hash,
                ):
                    return
                uow.commit()
        elif step is Step.RESET_TO_DRAFT:
            assert variant is not None
            if not withdraw_native(core, variant, now):
                report.busy += 1
                return
            variant = _move(
                core, variant, S.DRAFT, now, "row edited; approval withdrawn, checking again", d
            )
            if variant is None:
                return
            if sv and sv.variant.status in SCHEDULED:
                report.withdrawn += 1
        elif step is Step.MARK_INVALID:
            assert variant is not None
            variant = _move(core, variant, S.INVALID, now, reason or "invalid", d)
            report.marked_invalid += 1
            if variant is None:
                return
        elif step is Step.TO_DRAFT:
            assert variant is not None
            variant = _move(core, variant, S.DRAFT, now, "problems fixed", d)
            if variant is None:
                return
        elif step is Step.APPROVE:
            assert variant is not None and d.content is not None
            result, _ = approve(core, variant.id, d.content, SHEET_ACTOR)
            if result is RunResult.APPROVED:
                report.approved += 1
        elif step is Step.LEAVE_LIVE:
            report.left_live += 1
        elif step is Step.BUSY:
            report.busy += 1


def _create(sync: SyncServices, d: _Desired) -> Variant:
    assert d.post is not None and d.account is not None and d.publish_at is not None
    with sync.core.uow() as uow:
        post_id = uow.sync.upsert_post(sync.tenant_id, d.post.post_key, d.post.title)
        variant = Variant(
            id=str(uuid.uuid4()),
            tenant_id=sync.tenant_id,
            post_id=post_id,
            platform=d.row.platform,
            account_id=d.account.id,
            publish_at=d.publish_at,
        )
        uow.variants.add(variant, source_hash=d.source_hash)
        uow.commit()
    return variant


def _move(
    core: Services, variant: Variant, to: VariantStatus, now: datetime, reason: str, d: _Desired
) -> Variant | None:
    with core.uow() as uow:
        moved = move(
            uow,
            variant,
            to,
            reason=reason,
            at=now,
            next_step=None,
            actor=SHEET_ACTOR,
            publish_at=d.publish_at,
            source_hash=d.source_hash,
        )
        uow.commit()
    return moved


# --- writing back -----------------------------------------------------------------------------


def _status_plan(
    sync: SyncServices,
    posts: list[PostRow],
    desired: list[_Desired],
    ambiguous: set[Key],
    variants: list[SyncVariant],
    accounts: list[AccountInfo],
    now: datetime,
) -> StatusPlan:
    by_key = {(v.post_key, v.variant.platform, v.variant.account_id): v for v in variants}
    rows: list[RowStatus] = []
    shown: dict[str, list[tuple[str, str, str]]] = {}  # post_key -> (tab, status, error)

    for d in desired:
        sv = by_key.get(d.key) if d.key and d.key not in ambiguous else None
        status, url, error = row_status(
            enabled=d.row.enabled,
            variant_status=sv.variant.status if sv else None,
            problems=d.problems,
            external_url=sv.external_url if sv else None,
            external_id=sv.external_id if sv else None,
            last_reason=sv.last_reason if sv else None,
            edited_while_live=bool(
                sv and sv.variant.status is S.PUBLISHED and sv.source_hash != d.source_hash
            ),
        )
        shown.setdefault(d.row.post_key, []).append((d.row.tab, status, error))
        if changed_text(d.row.system, {"status": status, "live_url": url, "last_error": error}):
            rows.append(RowStatus(d.row.tab, d.row.row, status, url, error, now))

    post_rows: list[PostStatus] = []
    for post in posts:
        seen = shown.get(post.post_key, []) if post.post_key else []
        own = join_problems(post.problems)
        if not post.post_key:
            own = own or "post_key is required."
        elif sum(1 for p in posts if p.post_key == post.post_key) > 1:
            own = "This post_key appears more than once; keep one row."
        first = next(((tab, err) for tab, _, err in seen if err), None)
        error = own or (f"{first[0]}: {first[1]}" if first else "")
        summary = post_summary([status for _, status, _ in seen]) if not own else ""
        if changed_text(post.system, {"status": summary, "last_error": error}):
            post_rows.append(PostStatus(post.row, summary, error))

    calendar = [
        CalendarEntry(
            slot=v.variant.publish_at,
            platform=v.variant.platform,
            account=v.account_name or "",
            post_key=v.post_key,
            title=v.title or "",
            status=v.variant.status.value,
            source="human",
        )
        for v in variants
        if v.variant.status in CALENDAR_STATES
    ]
    names: dict[str, list[str]] = {p: [] for p in sync.platforms}
    for a in accounts:
        if a.status == "active" and a.display_name and a.platform in names:
            names[a.platform].append(a.display_name)
    return StatusPlan(rows=rows, posts=post_rows, calendar=calendar, accounts=names)
