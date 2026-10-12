"""Where publishing stands, for every post (Sheet and API): the overview and the calendar.

Read only. Both answers are JSON-ready dicts so every shell (HTTP, MCP) returns exactly the same
thing. States are the API's client-facing ones (`api_posts.STATE`), so a caller sees one vocabulary.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from dk_publishing.application.ports import SyncVariant
from dk_publishing.application.services import ApiServices
from dk_publishing.application.use_cases.api_posts import STATE, ApiError, utc_time
from dk_publishing.domain.publishing import ASSISTED_PREFIX
from dk_publishing.domain.status import VariantStatus

S = VariantStatus
PIPELINE = ("awaiting_approval", "scheduled", "publishing", "checking")  # not done yet
PROBLEMS = ("failed", "expired", "invalid", "checking")  # need a look
SOURCES = {"human": "sheet", "agent": "api"}
MAX_ROWS = 200
PREVIEW = 80


def _state(sv: SyncVariant) -> str:
    return STATE[sv.variant.status]


def _row(sv: SyncVariant) -> dict[str, Any]:
    content: Mapping[str, Any] = sv.content or {}
    text = str(content.get("caption") or content.get("title") or "")
    row: dict[str, Any] = {
        "post_key": sv.post_key,
        "platform": sv.variant.platform,
        "account": sv.account_name,
        "format": content.get("format"),
        "state": _state(sv),
        "slot": sv.variant.publish_at.isoformat(),
        "source": SOURCES.get(sv.source, sv.source),
        "by": sv.created_by,
        "text": text[:PREVIEW] + ("…" if len(text) > PREVIEW else ""),
        "live_url": sv.external_url,
        "handed_over": (sv.external_id or "").startswith(ASSISTED_PREFIX) or None,
        "error": sv.last_reason
        if sv.variant.status in (S.FAILED, S.EXPIRED, S.INVALID, S.UNKNOWN)
        else None,
    }
    return {k: v for k, v in row.items() if v not in (None, "", False)}


def _variants(api: ApiServices) -> list[SyncVariant]:
    with api.core.uow() as uow:
        return uow.sync.variants(api.tenant_id)


def overview(api: ApiServices, days_back: int = 7, days_ahead: int = 14) -> dict[str, Any]:
    """Counts, what is coming, what went out and what needs a look, in one small answer."""
    days_back, days_ahead = max(0, min(days_back, 90)), max(0, min(days_ahead, 366))
    now = api.core.clock.now()
    start, end = now - timedelta(days=days_back), now + timedelta(days=days_ahead)
    every = _variants(api)
    pipeline = [v for v in every if _state(v) in PIPELINE]
    recent = [v for v in every if start <= v.variant.publish_at <= now]
    published = [v for v in recent if _state(v) == "published"]
    problems = [v for v in every if _state(v) in PROBLEMS and v.variant.publish_at >= start]
    upcoming = sorted(
        (v for v in pipeline if now <= v.variant.publish_at <= end),
        key=lambda v: v.variant.publish_at,
    )
    last = max((v.variant.publish_at for v in pipeline), default=None)
    by_state: dict[str, Counter[str]] = {}
    for v in pipeline:
        by_state.setdefault(_state(v), Counter())[v.variant.platform] += 1
    return {
        "now": now.isoformat(),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "pipeline": {
            "total": len(pipeline),
            "by_state": {s: dict(sorted(c.items())) for s, c in sorted(by_state.items())},
        },
        "awaiting_approval": [_row(v) for v in pipeline if _state(v) == "awaiting_approval"][:20],
        "published": {
            "total": len(published),
            "by_platform": dict(sorted(Counter(v.variant.platform for v in published).items())),
        },
        "problems": [_row(v) for v in problems][:20],
        "next": [_row(v) for v in upcoming[:5]],
        "plan_ends": last.isoformat() if last else None,
        "note": f"Nothing is scheduled after {last:%Y-%m-%d %H:%M} UTC."
        if last
        else "Nothing is scheduled.",
    }


def calendar(
    api: ApiServices,
    *,
    state: str | None = None,
    platform: str | None = None,
    source: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Every post on every platform, by slot, narrowed by state, platform, source and dates.
    `state=published` is the history, with live links."""
    states = set(STATE.values())
    if state is not None and state not in states:
        raise ApiError("invalid", f"state must be one of: {', '.join(sorted(states))}.")
    if source is not None and source not in SOURCES.values():
        raise ApiError("invalid", "source must be sheet or api.")
    lo, hi = _bound(start, "from"), _bound(end, "to")
    rows = [
        v
        for v in _variants(api)
        if (state is None or _state(v) == state)
        and (platform is None or v.variant.platform == platform)
        and (source is None or SOURCES.get(v.source, v.source) == source)
        and (lo is None or v.variant.publish_at >= lo)
        and (hi is None or v.variant.publish_at <= hi)
    ]
    if state == "published":
        rows.reverse()  # history: newest first
    limit = max(1, min(limit, MAX_ROWS))
    return {"total": len(rows), "posts": [_row(v) for v in rows[:limit]], "limit": limit}


def _bound(text: str | None, name: str) -> datetime | None:
    if text is None:
        return None
    parsed = utc_time(text)
    if parsed is None:
        raise ApiError(
            "invalid",
            f"{name} must be an ISO 8601 date-time with an offset, e.g. 2026-10-12T00:00Z.",
        )
    return parsed
