"""Posts made through the HTTP API: check, submit, approve, cancel, look up.

The API is another way in, not another engine. A submitted variant becomes an ordinary draft;
approving it runs the same `approve` as the Sheet does, and from there the scheduler, the
duplicate guards and the platform adapters are untouched. Everything a client does wrong comes
back as a list of `Problem`s that say what is wrong, what is allowed and where to look, so a
caller (a program or an AI agent) can correct itself without a person explaining the rules.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dk_publishing.application.ports import (
    AccountInfo,
    DuplicatePost,
    NotifyError,
    StoredPost,
    SyncVariant,
    UploadedMedia,
)
from dk_publishing.application.services import ApiServices
from dk_publishing.application.use_cases._common import delivery_problems, move
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.native import withdraw_native
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.guide import FormatGuide, PlatformGuide
from dk_publishing.domain.model import Actor, ActorKind, Variant
from dk_publishing.domain.publishing import ASSISTED_PREFIX, VariantSnapshot, Violation
from dk_publishing.domain.snapshot import snapshot_hash
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.sync import SCHEDULED, cancel_path

S = VariantStatus

POST_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
MAX_VARIANTS = 20
MIN_LEAD = timedelta(minutes=1)
MAX_LEAD = timedelta(days=366)
GENERAL_FIELDS = ("format", "caption", "media")  # everything else a variant carries is an option

STATE = {
    S.DRAFT: "awaiting_approval",
    S.PENDING_APPROVAL: "awaiting_approval",
    S.INVALID: "invalid",
    S.APPROVED: "scheduled",
    S.PREPARING: "scheduled",
    S.PREPARED: "scheduled",
    S.SCHEDULING_NATIVE: "scheduled",
    S.SCHEDULED_NATIVE: "scheduled",
    S.PUBLISHING: "publishing",
    S.UNKNOWN: "checking",
    S.PUBLISHED: "published",
    S.FAILED: "failed",
    S.CANCELLED: "cancelled",
    S.EXPIRED: "expired",
}
SHOW_REASON = frozenset({S.INVALID, S.UNKNOWN, S.FAILED, S.EXPIRED, S.CANCELLED})


# --- what goes in and out ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantRequest:
    platform: str
    format: str
    caption: str | None
    media: tuple[str, ...]
    slot: str | None
    account: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PostRequest:
    post_key: str
    variants: tuple[VariantRequest, ...]
    label: str | None = None
    approve: bool = False


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing to fix. `path` points into the request, e.g. variants[1].caption."""

    path: str
    code: str
    message: str
    limit: int | None = None
    actual: int | None = None
    allowed: tuple[str, ...] = ()
    expected: str | None = None


@dataclass(frozen=True, slots=True)
class VariantView:
    variant_id: str
    platform: str
    account: str
    format: str | None
    state: str
    status: str
    slot: datetime
    live_url: str | None
    error: str | None
    handed_over: bool


@dataclass(frozen=True, slots=True)
class PostView:
    post_key: str
    label: str | None
    summary: str
    created_by: str | None
    variants: tuple[VariantView, ...]


class ApiError(Exception):
    """A request the API refuses. `kind` is one of invalid, conflict, not_found."""

    def __init__(
        self,
        kind: str,
        message: str,
        problems: Sequence[Problem] = (),
        examples: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.problems = tuple(problems)
        self.examples = dict(examples or {})


@dataclass(frozen=True, slots=True)
class Submitted:
    replayed: bool
    post: PostView


@dataclass(frozen=True, slots=True)
class _Planned:
    request: VariantRequest
    account: AccountInfo
    publish_at: datetime
    content: dict[str, Any]


@dataclass
class Checked:
    problems: list[Problem] = field(default_factory=list)
    examples: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    planned: list[_Planned] = field(default_factory=list)


# --- guide ------------------------------------------------------------------------------------


def platform_guides(api: ApiServices) -> list[tuple[PlatformGuide, str, list[str]]]:
    """Each platform that is switched on: its rules, its mode, and the accounts that can post."""
    with api.core.uow() as uow:
        accounts = uow.sync.accounts(api.tenant_id)
    return [
        (
            guide,
            api.modes[name],
            sorted(
                a.display_name or ""
                for a in accounts
                if a.platform == name and a.status == "active" and a.display_name
            ),
        )
        for name, guide in sorted(api.guides.items())
    ]


# --- checking ---------------------------------------------------------------------------------


def request_hash(request: PostRequest) -> str:
    """Identifies what was asked for, so a repeat of the same request is recognised."""
    return snapshot_hash(
        {
            "label": request.label or "",
            "variants": [
                {
                    "platform": v.platform,
                    "format": v.format,
                    "account": (v.account or "").strip().lower(),
                    "caption": v.caption,
                    "media": list(v.media),
                    "slot": _normal_slot(v.slot),
                    "options": dict(v.options),
                }
                for v in request.variants
            ],
        }
    )


def _normal_slot(text: str | None) -> str | None:
    parsed = _utc(text)
    return parsed.isoformat() if parsed else text


def check_post(api: ApiServices, request: PostRequest) -> Checked:
    """Everything wrong with the request, or the variants ready to be stored. Writes nothing."""
    now = api.core.clock.now()
    result = Checked()
    if not POST_KEY.match(request.post_key):
        result.problems.append(
            Problem(
                "post_key",
                "invalid_post_key",
                "post_key must be 1-80 characters: letters, digits, and . _ : - "
                "(starting with a letter or digit).",
                expected="for example campaign-42-reel",
            )
        )
    if not request.variants:
        result.problems.append(
            Problem("variants", "required", "Send at least one variant (one post on one platform).")
        )
    if len(request.variants) > MAX_VARIANTS:
        result.problems.append(
            Problem("variants", "too_many", f"At most {MAX_VARIANTS} variants per request.")
        )

    with api.core.uow() as uow:
        accounts = uow.sync.accounts(api.tenant_id)
        wanted = [m for v in request.variants for m in v.media]
        uploaded = uow.api.media(api.tenant_id, wanted)

    seen: set[tuple[str, str]] = set()
    for index, variant in enumerate(request.variants[:MAX_VARIANTS]):
        planned = _check_variant(api, index, variant, accounts, uploaded, now, result)
        if planned is None:
            continue
        pair = (planned.request.platform, planned.account.id)
        if pair in seen:
            result.problems.append(
                Problem(
                    f"variants[{index}]",
                    "duplicate_variant",
                    f"Another variant already targets {planned.request.platform} / "
                    f"{planned.account.display_name}. One post can have one variant per account.",
                )
            )
        seen.add(pair)
        result.planned.append(planned)
    return result


def _check_variant(
    api: ApiServices,
    index: int,
    variant: VariantRequest,
    accounts: Sequence[AccountInfo],
    uploaded: Mapping[str, UploadedMedia],
    now: datetime,
    result: Checked,
) -> _Planned | None:
    base = f"variants[{index}]"
    problems = result.problems
    before = len(problems)

    guide = api.guides.get(variant.platform)
    if guide is None:
        problems.append(
            Problem(
                f"{base}.platform",
                "unknown_platform",
                f"'{variant.platform}' is not available here. See GET /v1/platforms.",
                allowed=tuple(sorted(api.guides)),
            )
        )
        return None
    fmt = guide.format(variant.format)
    if fmt is None:
        problems.append(
            Problem(
                f"{base}.format",
                "unknown_format",
                f"{variant.platform} has no format '{variant.format}'.",
                allowed=tuple(f.name for f in guide.formats),
            )
        )
        return None

    known = {f.name for f in fmt.fields}
    for name in variant.options:
        if name not in known:
            problems.append(
                Problem(
                    f"{base}.options.{name}",
                    "unknown_option",
                    f"'{name}' is not an option of {variant.platform} {fmt.name}.",
                    allowed=tuple(sorted(known)),
                )
            )

    account = _account(variant, accounts, base, problems)
    media = _media(variant, uploaded, base, problems)
    publish_at = _slot(variant, now, base, problems)

    # Ask the platform's own rules too, so one round trip reports everything that is wrong. They
    # need the media; the slot and account only matter for the delivery check and the id.
    if len(media) == len(variant.media):
        content: dict[str, Any] = {
            "format": fmt.name,
            "media": media,
            **{k: v for k, v in variant.options.items() if k in known},
        }
        if variant.caption is not None:
            content["caption"] = variant.caption
        publisher = api.core.publishers.for_platform(variant.platform)
        probe = VariantSnapshot(
            "pending",
            api.tenant_id,
            variant.platform,
            account.id if account else "",
            publish_at or now,
            content,
        )
        violations = list(publisher.validate(probe))
        if publish_at is not None:
            violations += delivery_problems(publisher, content, publish_at, now)
        problems.extend(_explain(v, base, fmt, content) for v in violations)

    if len(problems) > before or account is None or publish_at is None:
        _attach_example(result, base, fmt, variant.platform)
        return None
    return _Planned(variant, account, publish_at, content)


def _attach_example(result: Checked, base: str, fmt: FormatGuide, platform: str) -> None:
    result.examples[base] = {
        "platform": platform,
        **fmt.example,
        "slot": "2026-10-12T19:30:00+02:00",
    }


def _account(
    variant: VariantRequest, accounts: Sequence[AccountInfo], base: str, problems: list[Problem]
) -> AccountInfo | None:
    active = [a for a in accounts if a.platform == variant.platform and a.status == "active"]
    names = tuple(sorted(a.display_name or "" for a in active))
    if variant.account:
        wanted = variant.account.strip().lower()
        found = next((a for a in active if (a.display_name or "").strip().lower() == wanted), None)
        if found is None:
            problems.append(
                Problem(
                    f"{base}.account",
                    "unknown_account",
                    f"No connected {variant.platform} account called '{variant.account}'.",
                    allowed=names,
                )
            )
        return found
    if len(active) == 1:
        return active[0]
    problems.append(
        Problem(
            f"{base}.account",
            "account_required" if active else "no_account",
            f"Say which {variant.platform} account to use."
            if active
            else f"No {variant.platform} account is connected yet.",
            allowed=names,
        )
    )
    return None


def _media(
    variant: VariantRequest,
    uploaded: Mapping[str, UploadedMedia],
    base: str,
    problems: list[Problem],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for position, media_id in enumerate(variant.media):
        found = uploaded.get(media_id)
        if found is None:
            problems.append(
                Problem(
                    f"{base}.media[{position}]",
                    "unknown_media",
                    f"'{media_id}' is not an uploaded file. Upload with POST /v1/media and use "
                    "the id it returns.",
                )
            )
            continue
        if found.purged:
            problems.append(
                Problem(
                    f"{base}.media[{position}]",
                    "media_expired",
                    f"'{found.filename}' was removed from the server (files are kept for a week "
                    "after their posts are done). Upload it again and use the new id.",
                )
            )
            continue
        items.append(
            {
                "name": found.filename,
                "media_id": found.id,
                "sha256": found.sha256,
                "size": found.size,
            }
        )
    return items


def _utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _slot(
    variant: VariantRequest, now: datetime, base: str, problems: list[Problem]
) -> datetime | None:
    path = f"{base}.slot"
    if not variant.slot:
        problems.append(
            Problem(
                path,
                "required",
                "slot is required: when to publish, e.g. 2026-10-12T19:30:00+02:00.",
            )
        )
        return None
    parsed = _utc(variant.slot)
    if parsed is None:
        problems.append(
            Problem(
                path,
                "invalid_slot",
                "slot must be an ISO 8601 date-time WITH a UTC offset, e.g. "
                "2026-10-12T19:30:00+02:00 or 2026-10-12T17:30:00Z. A time without an offset "
                "is ambiguous and is refused.",
            )
        )
        return None
    if parsed < now + MIN_LEAD:
        problems.append(
            Problem(path, "slot_in_past", "The slot must be at least a minute in the future.")
        )
        return None
    if parsed > now + MAX_LEAD:
        problems.append(Problem(path, "slot_too_far", "The slot is more than a year away."))
        return None
    return parsed


def _explain(
    violation: Violation, base: str, fmt: FormatGuide, content: Mapping[str, Any]
) -> Problem:
    """Turn an adapter's plain-language violation into a structured, self-explaining problem."""
    name = violation.field
    path = f"{base}.{name}" if name in GENERAL_FIELDS else f"{base}.options.{name}"
    guide = fmt.field(name)
    value = content.get(name)
    if name == "media":
        wants = fmt.media
        count = f"{wants.min}" if wants.min == wants.max else f"{wants.min} to {wants.max}"
        expected = f"{count} file(s) of type {', '.join(wants.types) or 'none'}"
        return Problem(path, "media_rejected", violation.message, expected=expected)
    if guide is None:
        return Problem(path, "invalid", violation.message)
    if guide.max_length is not None and isinstance(value, str):
        size = len(value.encode()) if name == "description" else len(value)
        if size > guide.max_length:
            return Problem(path, "too_long", violation.message, limit=guide.max_length, actual=size)
    if guide.choices and value not in guide.choices:
        return Problem(path, "not_allowed", violation.message, allowed=guide.choices)
    if guide.required and not value:
        return Problem(path, "required", violation.message)
    return Problem(path, "invalid", violation.message)


# --- submitting -------------------------------------------------------------------------------


def validate_post(api: ApiServices, request: PostRequest) -> Checked:
    """The same checks as `submit_post`, creating nothing. Also reports a clashing post_key."""
    result = check_post(api, request)
    with api.core.uow() as uow:
        existing = uow.api.post(api.tenant_id, request.post_key)
    if existing is not None and existing.request_hash != request_hash(request):
        result.problems.append(_taken(existing))
    return result


def _taken(existing: StoredPost) -> Problem:
    owner = "the Google Sheet" if existing.source != "agent" else "an earlier, different request"
    return Problem(
        "post_key",
        "post_key_taken",
        f"post_key '{existing.post_key}' is already used by {owner}. Use a new post_key; to "
        "change a post, cancel it (DELETE /v1/posts/{post_key}) and submit it again.",
    )


def submit_post(
    api: ApiServices, request: PostRequest, *, actor_name: str, may_approve: bool
) -> Submitted:
    """Create the post as drafts, or as approved when asked (and allowed). Safe to repeat: the
    same post_key with the same request returns the post that already exists."""
    digest = request_hash(request)
    with api.core.uow() as uow:
        existing = uow.api.post(api.tenant_id, request.post_key)
    if existing is not None:
        return _replay(api, existing, digest)

    if request.approve and not may_approve:
        raise ApiError(
            "forbidden",
            "This key may create drafts but not approve them. Send approve: false and have "
            "someone with approval rights approve the post.",
        )
    checked = check_post(api, request)
    if checked.problems:
        raise ApiError(
            "invalid",
            "The request has problems; nothing was created.",
            checked.problems,
            checked.examples,
        )

    try:
        with api.core.uow() as uow:
            post_id = uow.api.create_post(
                api.tenant_id, request.post_key, request.label, digest, actor_name
            )
            created: list[tuple[str, dict[str, Any]]] = []
            for item in checked.planned:
                variant = Variant(
                    id=str(uuid.uuid4()),
                    tenant_id=api.tenant_id,
                    post_id=post_id,
                    platform=item.request.platform,
                    account_id=item.account.id,
                    publish_at=item.publish_at,
                )
                uow.variants.add(
                    variant,
                    source_hash=snapshot_hash(
                        {"content": item.content, "slot": item.publish_at.isoformat()}
                    ),
                )
                uow.api.save_content(api.tenant_id, variant.id, item.content)
                created.append((variant.id, item.content))
            uow.commit()
    except DuplicatePost:  # a concurrent repeat won the race: answer as a repeat
        with api.core.uow() as uow:
            existing = uow.api.post(api.tenant_id, request.post_key)
        assert existing is not None
        return _replay(api, existing, digest)

    actor = Actor(ActorKind.AGENT, actor_name)
    if request.approve:
        for variant_id, content in created:
            approve(api.core, variant_id, content, actor)
    else:
        _tell_owner(api, request, actor_name)
    return Submitted(False, _view(api, request.post_key))


def _replay(api: ApiServices, existing: StoredPost, digest: str) -> Submitted:
    if existing.source != "agent" or existing.request_hash != digest:
        raise ApiError("conflict", _taken(existing).message, [_taken(existing)])
    return Submitted(True, _view(api, existing.post_key))


def _tell_owner(api: ApiServices, request: PostRequest, who: str) -> None:
    """A draft waits for a person; say so. Best effort: a Telegram outage must not fail the call."""
    if api.notifier is None:
        return
    platforms = ", ".join(sorted({v.platform for v in request.variants}))
    text = (
        f"📝 <b>New draft from {_escape(who)}</b>\n"
        f"{_escape(request.post_key)} → {_escape(platforms)}\n"
        f"Approve with: <code>dk api approve {_escape(request.post_key)}</code>"
    )
    with contextlib.suppress(NotifyError):
        api.notifier.send(text)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- approving and cancelling -----------------------------------------------------------------


def approve_post(api: ApiServices, post_key: str, *, actor_name: str) -> PostView:
    """Approve every draft variant of an API post. Refuses, changing nothing, if a slot passed."""
    _, variants = _own_post(api, post_key)
    drafts = [v for v in variants if v.variant.status in (S.DRAFT, S.PENDING_APPROVAL)]
    if not drafts:
        raise ApiError("conflict", "This post has nothing waiting for approval.")
    now = api.core.clock.now()
    late = [
        Problem(
            f"variants[{i}].slot",
            "slot_in_past",
            f"The {v.variant.platform} slot has passed. Cancel the post and submit it again.",
        )
        for i, v in enumerate(variants)
        if v in drafts and v.variant.publish_at < now + MIN_LEAD
    ]
    if late:
        raise ApiError("invalid", "A slot has passed; nothing was approved.", late)
    actor = Actor(ActorKind.AGENT, actor_name)
    with api.core.uow() as uow:
        contents = {v.variant.id: uow.api.content(v.variant.id) for v in drafts}
    problems: list[Problem] = []
    for index, sv in enumerate(variants):
        content = contents.get(sv.variant.id)
        if sv not in drafts or content is None:
            continue
        outcome, violations = approve(api.core, sv.variant.id, content, actor)
        if outcome is RunResult.INVALID:
            fmt = _format_of(api, sv.variant.platform, content)
            for x in violations:
                path = f"variants[{index}]"
                problems.append(
                    _explain(x, path, fmt, content) if fmt else Problem(path, "invalid", x.message)
                )
    if problems:
        raise ApiError("invalid", "Some variants could not be approved.", problems)
    return _view(api, post_key)


def cancel_post(api: ApiServices, post_key: str, *, actor_name: str) -> PostView:
    """Cancel every variant that has not gone out. Published or in-flight variants are left."""
    _, variants = _own_post(api, post_key)
    now = api.core.clock.now()
    actor = Actor(ActorKind.AGENT, actor_name)
    stuck = 0
    for sv in variants:
        status = sv.variant.status
        if status not in SCHEDULED | {S.DRAFT, S.INVALID}:
            stuck += 0 if status in (S.CANCELLED, S.FAILED, S.EXPIRED) else 1
            continue
        if not withdraw_native(api.core, sv.variant, now):
            stuck += 1
            continue
        with api.core.uow() as uow:
            moved: Variant | None = sv.variant
            for target in cancel_path(status):
                assert moved is not None
                moved = move(
                    uow, moved, target, reason="cancelled through the API", at=now,
                    next_step=None, actor=actor,
                )  # fmt: skip
                if moved is None:
                    break
            uow.commit()
        if moved is None:
            stuck += 1
    if stuck:
        raise ApiError(
            "conflict",
            f"{stuck} variant(s) are already publishing or published and cannot be cancelled. "
            "The others were cancelled; see GET /v1/posts/{post_key}.",
        )
    return _view(api, post_key)


# --- looking things up ------------------------------------------------------------------------


def get_post(api: ApiServices, post_key: str) -> PostView:
    return _view(api, post_key)


def list_posts(api: ApiServices, limit: int = 50) -> list[PostView]:
    with api.core.uow() as uow:
        stored = uow.api.posts(api.tenant_id, limit)
        variants = uow.sync.variants(api.tenant_id)
        contents = {
            v.variant.id: uow.api.content(v.variant.id) for v in variants if v.source == "agent"
        }
    return [_build(p, [v for v in variants if v.post_key == p.post_key], contents) for p in stored]


def _own_post(api: ApiServices, post_key: str) -> tuple[StoredPost, list[SyncVariant]]:
    with api.core.uow() as uow:
        post = uow.api.post(api.tenant_id, post_key)
        variants = (
            [v for v in uow.sync.variants(api.tenant_id) if v.post_key == post_key] if post else []
        )
    if post is None:
        raise ApiError("not_found", f"No post '{post_key}'.")
    if post.source != "agent":
        raise ApiError("conflict", "That post belongs to the Google Sheet, not to the API.")
    return post, variants


def _view(api: ApiServices, post_key: str) -> PostView:
    post, variants = _own_post(api, post_key)
    with api.core.uow() as uow:
        contents = {v.variant.id: uow.api.content(v.variant.id) for v in variants}
    return _build(post, variants, contents)


def _build(
    post: StoredPost,
    variants: Sequence[SyncVariant],
    contents: Mapping[str, Mapping[str, Any] | None],
) -> PostView:
    views = tuple(
        VariantView(
            variant_id=sv.variant.id,
            platform=sv.variant.platform,
            account=sv.account_name or "",
            format=str((contents.get(sv.variant.id) or {}).get("format") or "") or None,
            state=STATE[sv.variant.status],
            status=sv.variant.status.value,
            slot=sv.variant.publish_at,
            live_url=sv.external_url,
            error=sv.last_reason if sv.variant.status in SHOW_REASON else None,
            handed_over=bool((sv.external_id or "").startswith(ASSISTED_PREFIX)),
        )
        for sv in sorted(variants, key=lambda v: (v.variant.publish_at, v.variant.platform))
    )
    return PostView(post.post_key, post.label, _summary(views), post.created_by, views)


def _summary(views: Sequence[VariantView]) -> str:
    states = {v.state for v in views}
    for state in ("failed", "invalid", "checking", "publishing", "awaiting_approval", "scheduled"):
        if state in states:
            return state
    live = states - {"cancelled", "expired"}
    if not live:
        return "cancelled" if "cancelled" in states else "expired"
    return "published"


def _format_of(api: ApiServices, platform: str, content: Mapping[str, Any]) -> FormatGuide | None:
    guide = api.guides.get(platform)
    return guide.format(str(content.get("format"))) if guide else None
