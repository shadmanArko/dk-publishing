"""`dk meta check`: what can this file's tokens actually do? Nothing here ever posts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from dk_publishing.adapters.config.platforms import PlatformSettings
from dk_publishing.adapters.platforms.live import DEFAULT_GRAPH_VERSION
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials, SectionTokenProvider
from dk_publishing.domain.errors import PublishingError

FACEBOOK_SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts")


@dataclass
class MetaReport:
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.lines.append(f"[ok]   {text}")

    def note(self, text: str) -> None:
        self.lines.append(f"[todo] {text}")

    def fail(self, text: str) -> None:
        self.problems.append(text)
        self.lines.append(f"[FAIL] {text}")


class _Static:
    def __init__(self, value: str) -> None:
        self._value = value

    def token(self) -> str:
        return self._value


def check_facebook(
    credentials: MetaCredentials, *, version: str, transport: httpx.BaseTransport | None = None
) -> MetaReport:
    report = MetaReport()
    section = credentials.section("facebook")
    page_id = str(section.get("page_id") or "").strip()
    if not page_id:
        report.fail("facebook.page_id is empty in the credentials file")
        return report
    if not str(section.get("access_token") or "").strip():
        report.note(f"facebook.access_token is empty: paste the Page token into {credentials.path}")
        return report

    graph = GraphClient(
        version=version, tokens=SectionTokenProvider(credentials, "facebook"), transport=transport
    )
    try:
        page = graph.get(page_id, {"fields": "id,name"})
        report.ok(f"the token works for the Page {page.get('name')!r} (id {page.get('id')})")
        graph.get(f"{page_id}/feed", {"limit": "1"})
        report.ok("it can read the Page's posts (needed to confirm a post went out)")
    except PublishingError as exc:
        report.fail(f"Facebook refused the token: {exc}")
        return report

    app_id, secret = credentials.value("app_id"), credentials.value("app_secret")
    if not (app_id and secret):
        report.note("add app_secret to see exactly which permissions the token has")
        return report
    try:
        info = GraphClient(
            version=version, tokens=_Static(f"{app_id}|{secret}"), transport=transport
        ).get(
            "debug_token", {"input_token": SectionTokenProvider(credentials, "facebook").token()}
        )["data"]
    except PublishingError as exc:
        report.fail(f"could not inspect the token: {exc}")
        return report

    scopes = set(info.get("scopes") or [])
    missing = [s for s in FACEBOOK_SCOPES if s not in scopes]
    if missing:
        report.fail(
            f"the token lacks permission: {', '.join(missing)}. Generate it again with them ticked."
        )
    else:
        report.ok(f"it has the permissions posting needs ({', '.join(FACEBOOK_SCOPES)})")
    if info.get("type") != "PAGE":
        report.fail(
            f"this is a {info.get('type')} token; posting needs a PAGE token (use GET /me/accounts)"
        )
    expires = int(info.get("expires_at") or 0)
    if expires:
        left = datetime.fromtimestamp(expires, UTC) - datetime.now(UTC)
        report.ok(f"it expires in {left.days} days") if left.days > 7 else report.fail(
            f"it expires in {left.days} days; make a long-lived one"
        )
    else:
        report.ok("it does not expire")
    return report


def check_meta(
    credentials: MetaCredentials,
    platforms: Mapping[str, PlatformSettings],
    transport: httpx.BaseTransport | None = None,
) -> MetaReport:
    settings = platforms.get("facebook")
    version = (settings.api_version if settings else None) or DEFAULT_GRAPH_VERSION
    return check_facebook(credentials, version=version, transport=transport)
