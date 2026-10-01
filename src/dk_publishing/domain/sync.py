"""Pure rules for the Sheet sync: what to do with a row, and what to tell the person.

Nothing here reads a Sheet or a database. The use case gathers facts and applies the steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from dk_publishing.domain.publishing import Violation
from dk_publishing.domain.sheet import MediaFile, PlatformRow, PostRow
from dk_publishing.domain.snapshot import snapshot_hash
from dk_publishing.domain.status import IN_FLIGHT, VariantStatus
from dk_publishing.domain.timezones import InvalidLocalTime, berlin_to_utc

S = VariantStatus

LIVE_EDIT_NOTE = "This post is already live; edits are ignored."
# Moves a person can still undo or withdraw. Counting these guards against deleting a block of
# rows by accident.
SCHEDULED = frozenset({S.APPROVED, S.PREPARED, S.SCHEDULED_NATIVE})
CANCELLABLE = SCHEDULED | {S.DRAFT, S.INVALID}
# Someone else (a run, a reconciliation, an approval tap) owns the variant right now.
BUSY = IN_FLIGHT | {S.UNKNOWN, S.PENDING_APPROVAL}
SETTLED = frozenset({S.FAILED, S.CANCELLED, S.EXPIRED})


class Step(StrEnum):
    CREATE = "create"
    UPDATE_INPUTS = "update_inputs"  # a draft or invalid variant: just record the new inputs
    RESET_TO_DRAFT = "reset_to_draft"  # an edit after approval, or a fresh draft after a failure
    MARK_INVALID = "mark_invalid"
    TO_DRAFT = "to_draft"  # the problems were fixed
    APPROVE = "approve"
    CANCEL = "cancel"
    LEAVE_LIVE = "leave_live"  # already published: edits are ignored
    BUSY = "busy"  # try again on the next sync


def plan_steps(
    existing: VariantStatus | None, *, changed: bool, enabled: bool, ready: bool, valid: bool
) -> list[Step]:
    """The ordered steps for one (post, platform, account), given its state and the row's.

    `changed` means the row's inputs differ from those the variant was last synced with. The
    decision table follows the plan's "Edits after approval" section.
    """
    if existing is None:
        return [Step.CREATE, *_settle(S.DRAFT, ready, valid)] if enabled else []
    if existing is S.PUBLISHED:
        return [Step.LEAVE_LIVE]
    if existing in BUSY:
        return [Step.BUSY]
    if not enabled:
        return [Step.CANCEL] if existing in CANCELLABLE else []

    steps: list[Step] = []
    status = existing
    if changed:
        if status in SCHEDULED or status in SETTLED:
            steps.append(Step.RESET_TO_DRAFT)
            status = S.DRAFT
        else:
            steps.append(Step.UPDATE_INPUTS)
    elif status in SCHEDULED or status in SETTLED:
        return []  # approved and waiting, or finished: nothing was edited, so nothing to do
    return [*steps, *_settle(status, ready, valid)]


def _settle(status: VariantStatus, ready: bool, valid: bool) -> list[Step]:
    """From a draft or invalid variant: stop at invalid, wait as a draft, or go on to approval."""
    if not valid:
        return [] if status is S.INVALID else [Step.MARK_INVALID]
    steps = [Step.TO_DRAFT] if status is S.INVALID else []
    return [*steps, Step.APPROVE] if ready else steps


def cancel_path(status: VariantStatus) -> list[VariantStatus]:
    """The lifecycle moves that end in cancelled (an invalid variant must pass through draft)."""
    if status is S.INVALID:
        return [S.DRAFT, S.CANCELLED]
    return [S.CANCELLED]


# --- slots and media --------------------------------------------------------------------------


def resolve_slot(local: datetime | None, now: datetime) -> tuple[datetime | None, list[Violation]]:
    """Berlin wall-clock time to a UTC instant, or the reason it cannot be one."""
    if local is None:
        return None, [
            Violation("slot", "No slot: fill slot on this tab, or default_slot on Posts.")
        ]
    try:
        utc = berlin_to_utc(local)
    except InvalidLocalTime as exc:
        return None, [Violation("slot", str(exc))]
    if utc <= now:
        return utc, [Violation("slot", f"The slot {local:%d.%m.%Y %H:%M} is in the past.")]
    return utc, []


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    name: str
    file_id: str | None
    md5: str | None


def parse_media(text: str) -> tuple[str, ...]:
    return tuple(name.strip() for name in text.split(",") if name.strip())


def resolve_media(
    names: Sequence[str], files: Sequence[MediaFile]
) -> tuple[list[ResolvedMedia], list[Violation]]:
    """Match file names to Drive files. A missing or ambiguous name is an error, never a guess."""
    resolved: list[ResolvedMedia] = []
    problems: list[Violation] = []
    for name in names:
        matches = [f for f in files if f.name == name]
        if len(matches) == 1:
            resolved.append(ResolvedMedia(name, matches[0].id, matches[0].md5))
            continue
        resolved.append(ResolvedMedia(name, None, None))
        if not matches:
            problems.append(
                Violation("media", f"Media file '{name}' was not found in the Drive folder.")
            )
        else:
            problems.append(
                Violation(
                    "media",
                    f"{len(matches)} files named '{name}' are in the Drive folder; "
                    "rename all but one.",
                )
            )
    return resolved, problems


# --- what the variant is made of --------------------------------------------------------------


def effective_caption(post: PostRow | None, row: PlatformRow) -> str:
    return row.caption or (post.default_caption if post else "")


def effective_slot(post: PostRow | None, row: PlatformRow) -> datetime | None:
    return row.slot or (post.default_slot if post else None)


def content_for(
    post: PostRow | None, row: PlatformRow, media: Sequence[ResolvedMedia]
) -> dict[str, Any]:
    """The content that gets frozen at approval and hashed. The slot is kept out on purpose: it
    lives on the variant, and changing it goes through the same edit path."""
    return {
        "caption": effective_caption(post, row),
        "media": [{"name": m.name, "drive_file_id": m.file_id, "md5": m.md5} for m in media],
        **dict(row.options),
    }


def source_hash(post: PostRow | None, row: PlatformRow, media: Sequence[ResolvedMedia]) -> str:
    """Everything a person can edit that affects this variant. The title is left out because it
    is never published, so renaming a post must not withdraw its approval."""
    slot = effective_slot(post, row)
    return snapshot_hash(
        {
            "enabled": row.enabled,
            "account": row.account.strip().lower(),
            "caption": effective_caption(post, row),
            "slot": slot.isoformat() if slot else None,
            "options": dict(row.options),
            "ready": bool(post and post.ready),
            "media_names": list(post.media) if post else [],
            "media": [[m.name, m.md5] for m in media],
        }
    )


# --- what to write back -----------------------------------------------------------------------


def join_problems(problems: Sequence[Violation]) -> str:
    return " ".join(p.message for p in problems)


def row_status(
    *,
    enabled: bool,
    variant_status: VariantStatus | None,
    problems: Sequence[Violation],
    external_url: str | None,
    last_reason: str | None,
    edited_while_live: bool,
) -> tuple[str, str, str]:
    """(status, live_url, last_error) in plain words for one platform row."""
    if variant_status is None:
        return ("invalid", "", join_problems(problems)) if enabled and problems else ("", "", "")
    status = variant_status.value
    if variant_status is S.PUBLISHED:
        return status, external_url or "", LIVE_EDIT_NOTE if edited_while_live else ""
    if variant_status in (S.DRAFT, S.INVALID):
        return status, "", join_problems(problems)
    if variant_status in (S.FAILED, S.EXPIRED):
        return status, "", last_reason or ""
    return status, "", ""


def post_summary(statuses: Sequence[str]) -> str:
    """The roll-up shown on the Posts tab, e.g. '2 of 3 live, 1 failed'."""
    counted = [s for s in statuses if s and s != "cancelled"]
    if not counted:
        return ""
    live = counted.count("published")
    parts = [f"{live} of {len(counted)} live"]
    for label, matching in (
        ("failed", ("failed", "expired")),
        ("invalid", ("invalid",)),
        ("waiting", ("draft",)),
    ):
        n = sum(counted.count(m) for m in matching)
        if n:
            parts.append(f"{n} {label}")
    return ", ".join(parts)


def changed_text(old: Mapping[str, Any], new: Mapping[str, str]) -> bool:
    """Did any of the status cells differ from what the Sheet already shows?"""
    return any(str(old.get(key, "") or "") != value for key, value in new.items())
