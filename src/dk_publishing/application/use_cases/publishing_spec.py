"""The publishing contract: one document that tells any producer (a script, a media agent, a
writer agent, an orchestrator) exactly what this system accepts, before it makes anything.

It is assembled from what the engine enforces: the platform guides (proven against the adapters'
`validate`), the platform settings (mode, lead times, lateness, native window), the connected
accounts and the API's own request rules. Nothing here is a second copy of a rule. `spec_version` is
a hash of the whole document, so a client can cache it and notice when it changes.
"""

from __future__ import annotations

from typing import Any

from dk_publishing.application.services import ApiServices
from dk_publishing.application.use_cases.api_posts import (
    MAX_LEAD,
    MAX_VARIANTS,
    MIN_LEAD,
    POST_KEY,
    STATE,
    ApiError,
    request_example,
)
from dk_publishing.domain.guide import FieldGuide, FormatGuide
from dk_publishing.domain.snapshot import snapshot_hash
from dk_publishing.domain.spec import SPEC_FORMAT, PlatformFacts, variant_schema

# What each state means to a client, and what it should do. Every API state has an entry (tested).
STATES: dict[str, dict[str, str]] = {
    "awaiting_approval": {
        "means": "Created; waits for a person to approve it in the Calendar.",
        "do": "Nothing. To change it, cancel it and submit again with a new post_key.",
    },
    "scheduled": {
        "means": "Approved; it will be published at its slot.",
        "do": "Nothing.",
    },
    "publishing": {"means": "Being published right now.", "do": "Nothing."},
    "checking": {
        "means": "The outcome is uncertain; the system is asking the platform what happened.",
        "do": "Never resend. Look again later; it becomes published or failed by itself.",
    },
    "published": {
        "means": "Live; live_url points to it. handed_over: a person was given it to post by hand.",
        "do": "Nothing.",
    },
    "invalid": {
        "means": "A rule failed when it was approved; error says which.",
        "do": "Fix it and submit with a new post_key.",
    },
    "failed": {
        "means": "It did not go out; error says why.",
        "do": "Fix the cause and submit with a new post_key.",
    },
    "cancelled": {"means": "Cancelled before it went out.", "do": "Nothing."},
    "expired": {
        "means": "The slot passed by more than the platform's lateness limit before it went out.",
        "do": "Submit again with a new slot and a new post_key.",
    },
}

ROUTE_NOT_LIVE = {
    "assisted": "Handed to a person on Telegram at the slot, who posts it by hand.",
    "dry_run": "Rehearsal: recorded here only; nothing reaches the platform.",
}


def _seconds(value: Any) -> int:
    return int(value.total_seconds())


def _field(f: FieldGuide) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": f.kind, "required": f.required}
    if f.choices:
        out["choices"] = list(f.choices)
    if f.max_length is not None:
        out["max_length"] = f.max_length
    if f.note:
        out["note"] = f.note
    return out


def _format(platform: str, fmt: FormatGuide, accounts: list[str]) -> dict[str, Any]:
    media: dict[str, Any] = {
        "min": fmt.media.min,
        "max": fmt.media.max,
        "types": list(fmt.media.types),
    }
    if fmt.media.max_bytes is not None:
        media["max_bytes"] = fmt.media.max_bytes
    if fmt.media.note:
        media["note"] = fmt.media.note
    return {
        "summary": fmt.summary,
        "media": media,
        "caption": _field(fmt.caption) if fmt.caption else None,
        "options": {f.name: _field(f) for f in fmt.fields},
        "example": request_example(platform, fmt.example),
        "json_schema": variant_schema(platform, fmt, accounts),
    }


def _route(mode: str, facts: PlatformFacts | None) -> dict[str, Any]:
    if mode != "live":
        return {"how": ROUTE_NOT_LIVE.get(mode, mode)}
    route: dict[str, Any] = {"how": facts.route_live if facts else "The platform's own API."}
    if facts and facts.api_version:
        route["api_version"] = facts.api_version
    return route


def build_spec(api: ApiServices) -> dict[str, Any]:
    """The whole contract for the platforms that are switched on here."""
    with api.core.uow() as uow:
        accounts = uow.sync.accounts(api.tenant_id)
    platforms: dict[str, Any] = {}
    for name, guide in sorted(api.guides.items()):
        names = sorted(
            a.display_name
            for a in accounts
            if a.platform == name and a.status == "active" and a.display_name
        )
        caps = api.core.publishers.for_platform(name).capabilities
        facts = api.facts.get(name)
        timing: dict[str, Any] = {
            "prepare_lead_s": _seconds(caps.prepare_lead),
            "max_lateness_s": _seconds(caps.max_lateness),
        }
        if caps.native_window is not None:
            timing["native_window_s"] = [
                _seconds(caps.native_window[0]),
                _seconds(caps.native_window[1]),
            ]
        platforms[name] = {
            "summary": guide.summary,
            "mode": api.modes[name],
            "route": _route(api.modes[name], facts),
            "accounts": names,
            "timing": timing,
            "quotas": list(facts.quotas) if facts else [],
            "notes": list(guide.notes),
            "formats": {f.name: _format(name, f, names) for f in guide.formats},
        }
    body: dict[str, Any] = {
        "spec": SPEC_FORMAT,
        "post": {
            "post_key_pattern": POST_KEY.pattern,
            "variants": {"min": 1, "max": MAX_VARIANTS, "one_per_account": True},
            "slot": {
                "rule": "ISO 8601 with a UTC offset; a time without an offset is refused.",
                "min_lead_s": _seconds(MIN_LEAD),
                "max_lead_s": _seconds(MAX_LEAD),
            },
            "idempotency": "post_key identifies a post: the same post_key with the same content "
            "returns the existing post, never a second one; with different content it is refused.",
            "approval": "Every post is a draft until a person approves it in the Calendar.",
            "change": "Posts are not edited in place: cancel and submit again with a new post_key.",
        },
        "upload": {
            "how": [
                "POST /v1/media with the file as the body and ?filename=name.ext",
                'POST /v1/media with JSON {"url": "https://...", "filename": "name.ext"}: '
                "a public https link the server downloads",
            ],
            "types": list(api.upload_types),
            "max_file_bytes": api.max_upload_bytes or None,
            "quota_per_key_bytes": api.key_quota_bytes or None,
            "kept": "Files are kept until a week after the posts that use them are done.",
        },
        "states": {state: STATES[state] for state in sorted(set(STATE.values()))},
        "platforms": platforms,
    }
    return {"spec_version": snapshot_hash(body)[:16], **body}


def spec_slice(
    spec: dict[str, Any], platform: str | None = None, fmt: str | None = None
) -> dict[str, Any]:
    """The contract narrowed to one platform (and format): small enough for an agent's context."""
    if platform is None:
        if fmt is not None:
            raise ApiError("invalid", "Name a platform to narrow to one format.")
        return spec
    found = spec["platforms"].get(platform)
    if found is None:
        raise ApiError(
            "not_found",
            f"No platform '{platform}' here. Available: {', '.join(spec['platforms'])}.",
        )
    if fmt is not None:
        if fmt not in found["formats"]:
            raise ApiError(
                "not_found",
                f"{platform} has no format '{fmt}'. Available: {', '.join(found['formats'])}.",
            )
        found = {**found, "formats": {fmt: found["formats"][fmt]}}
    return {**spec, "platforms": {platform: found}}
