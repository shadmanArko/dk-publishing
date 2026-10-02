"""`dk meta check`: what can this file's tokens actually do? Nothing here ever posts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings
from dk_publishing.adapters.platforms.live import DEFAULT_GRAPH_VERSION
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials, SectionTokenProvider
from dk_publishing.adapters.platforms.threads import DEFAULT_VERSION as THREADS_VERSION
from dk_publishing.adapters.platforms.threads import HOST as THREADS_HOST
from dk_publishing.adapters.platforms.threads_token import WARN_WITHIN, days_left
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
    except PublishingError as exc:
        report.fail(f"Facebook refused the token: {exc}")
        return report
    try:
        # A Page token answers /me as the Page itself; a user token answers as the person.
        me = graph.get("me", {"fields": "id,name"})
    except PublishingError:
        me = {}
    if me and str(me.get("id")) != page_id:
        report.fail(
            f"this is not the Page's own token: it belongs to {me.get('name')!r}. Posting needs "
            "the "
            "Page token: with this token in the Graph API Explorer run `me/accounts` and copy the "
            "access_token of the entry whose id is the Page's."
        )
    try:
        graph.get(f"{page_id}/published_posts", {"limit": "1"})
        report.ok("it can read the Page's published posts (needed to confirm a post went out)")
    except PublishingError as exc:
        # Not fatal for the rest of the check: carry on so the permissions get listed below.
        report.fail(
            "it cannot read the Page's published posts, which is how a post is confirmed after a "
            f"lost answer. Facebook said: {exc}"
        )

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
    report = check_facebook(credentials, version=version, transport=transport)
    threads = platforms.get("threads")
    try:
        extra = check_threads(
            credentials, (threads.api_version if threads else None) or THREADS_VERSION, transport
        )
    except ConfigError:  # no threads section in an older file: nothing to check
        return report
    report.lines.extend(extra.lines)
    report.problems.extend(extra.problems)
    instagram = platforms.get("instagram")
    try:
        more = check_instagram(
            credentials,
            (instagram.api_version if instagram else None) or DEFAULT_GRAPH_VERSION,
            transport,
        )
    except ConfigError:  # no instagram section in an older file: nothing to check
        return report
    report.lines.extend(more.lines)
    report.problems.extend(more.problems)
    return report


@dataclass
class SwapResult:
    ok: bool
    changed: bool
    message: str


def swap_for_page_token(
    credentials: MetaCredentials,
    platforms: Mapping[str, PlatformSettings],
    transport: httpx.BaseTransport | None = None,
) -> SwapResult:
    """Replace a user token in the file with the Page's own token, fetched from Facebook.

    Posting needs the Page token; people tend to paste the user token they generated it from.
    The user token is kept as `user_access_token`, so this is reversible and repeatable.
    """
    settings = platforms.get("facebook")
    version = (settings.api_version if settings else None) or DEFAULT_GRAPH_VERSION
    section = credentials.section("facebook")
    page_id = str(section.get("page_id") or "").strip()
    user_token = str(section.get("access_token") or "").strip()
    if not page_id or not user_token:
        return SwapResult(
            False, False, "facebook.page_id and facebook.access_token must both be set"
        )

    graph = GraphClient(
        version=version, tokens=SectionTokenProvider(credentials, "facebook"), transport=transport
    )
    try:
        me = graph.get("me", {"fields": "id,name"})
        if str(me.get("id")) == page_id:
            return SwapResult(True, False, "this is already the Page's own token; nothing to do")
        accounts = graph.get("me/accounts", {"fields": "id,name,access_token", "limit": "200"})
    except PublishingError as exc:
        return SwapResult(False, False, f"Facebook refused the token: {exc}")

    pages = accounts.get("data") or []
    match = next((p for p in pages if str(p.get("id")) == page_id), None)
    if match is None or not match.get("access_token"):
        seen = ", ".join(repr(p.get("name")) for p in pages) or "no Pages at all"
        return SwapResult(
            False,
            False,
            f"this token can see {seen}, but not the Page with id {page_id}. The account that made "
            "it must be an admin of the Page, and the token needs pages_show_list. If the Page "
            "belongs to a Business portfolio, the Explorer may also need business_management.",
        )
    credentials.update_section(
        "facebook",
        {"access_token": match["access_token"], "user_access_token": user_token},
    )
    return SwapResult(
        True,
        True,
        f"replaced the user token with the Page token for {match.get('name')!r}. Your user token "
        "is kept in the file as facebook.user_access_token",
    )


def check_threads(
    credentials: MetaCredentials,
    version: str = THREADS_VERSION,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> MetaReport:
    report = MetaReport()
    section = credentials.section("threads")
    user_id = str(section.get("user_id") or "").strip()
    if not user_id:
        report.fail("threads.user_id is empty in the credentials file")
        return report
    if not str(section.get("access_token") or "").strip():
        report.note(
            f"threads.access_token is empty: paste the Threads token into {credentials.path}"
        )
        return report
    graph = GraphClient(
        version=version,
        tokens=SectionTokenProvider(credentials, "threads"),
        transport=transport,
        host=THREADS_HOST,
        video_host=THREADS_HOST,
    )
    try:
        me = graph.get("me", {"fields": "id,username"})
    except PublishingError as exc:
        report.fail(f"Threads refused the token: {exc}")
        return report
    if str(me.get("id")) != user_id:
        report.fail(f"this token belongs to Threads user {me.get('id')}, not {user_id}")
    else:
        report.ok(f"the Threads token works for @{me.get('username')} (id {user_id})")
    left = days_left(credentials, now)
    if left is None:
        report.note("the Threads token's expiry is unknown; run `make meta-refresh` to renew it")
    elif left < WARN_WITHIN.days:
        report.fail(f"the Threads token expires in {left} days; run `make meta-refresh`")
    else:
        report.ok(f"the Threads token has {left} days left")
    return report


INSTAGRAM_SCOPES = ("instagram_basic", "instagram_content_publish", "pages_read_engagement")


def check_instagram(
    credentials: MetaCredentials,
    version: str = DEFAULT_GRAPH_VERSION,
    transport: httpx.BaseTransport | None = None,
) -> MetaReport:
    report = MetaReport()
    section = credentials.section("instagram")
    account_id = str(section.get("account_id") or "").strip()
    if not account_id:
        report.fail("instagram.account_id is empty in the credentials file")
        return report
    provider = SectionTokenProvider(credentials, "instagram", fallback="facebook")
    try:
        provider.token()
    except PublishingError:
        report.note("no Instagram token yet: it uses the Facebook Page token, so paste that first")
        return report
    graph = GraphClient(version=version, tokens=provider, transport=transport)
    try:
        account = graph.get(account_id, {"fields": "id,username"})
        report.ok(
            f"the token works for Instagram @{account.get('username')} (id {account.get('id')})"
        )
    except PublishingError as exc:
        report.fail(
            f"Instagram refused the token: {exc}. The Page token needs instagram_basic and "
            "instagram_content_publish, and the account must be a professional account linked "
            "to the Page."
        )
        return report
    try:
        data = graph.get(f"{account_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        row = (data.get("data") or [{}])[0]
        report.ok(
            f"it can publish: {row.get('quota_usage', '?')} of "
            f"{(row.get('config') or {}).get('quota_total', '?')} API posts used in the last 24 hours"
        )
    except PublishingError as exc:
        report.fail(f"it cannot publish to Instagram (needs instagram_content_publish): {exc}")
    return report
