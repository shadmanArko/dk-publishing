"""The owner's daily review page (ADR 0025): `/review`, opened from a signed link.

A thin shell like the rest of the API: it checks who is asking, then calls the same use cases the
Sheet uses (`daily_review.load_day`, `daily_review.decide` -> `review_drafts`). Nothing here knows
what makes a post approvable. Not part of the OpenAPI document: this is a page, not an API.

No `from __future__ import annotations` here: FastAPI reads parameter annotations at runtime.
"""

import logging
from datetime import date
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from dk_publishing.application.services import ReviewServices
from dk_publishing.application.use_cases import daily_review
from dk_publishing.domain import review as tokens
from dk_publishing.domain.timezones import utc_to_berlin
from dk_publishing.entrypoints.publish_api import review_pages as pages

COOKIE = "dk_review"
MAX_FORM_BYTES = 64 * 1024
MAX_DAYS_AWAY = 120  # the picker is for the next days, not for archaeology
HEADERS = {
    "Cache-Control": "no-store",
    # same-origin, not no-referrer: with no-referrer Chrome sends "Origin: null" on this page's
    # own form posts and the same-site check rightly refuses "null" (found live 2026-10-13: every
    # Approve and Reject was refused). A cross-site request carries no referrer either way.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": pages.CSP,
}


class HideLinkToken(logging.Filter):
    """The web server's access log would print `/review/enter?t=<token>`. Cut the query off."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and "/review/enter" in str(args[2]):
            record.args = (*args[:2], "/review/enter?…", *args[3:])
        return True


def _html(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers=HEADERS)


def _today(services: ReviewServices) -> date:
    return utc_to_berlin(services.core.clock.now()).date()


def _day(text: str | None, today: date) -> date:
    try:
        wanted = date.fromisoformat(text or "")
    except ValueError:
        return today
    return wanted if abs((wanted - today).days) <= MAX_DAYS_AWAY else today


async def _form(request: Request) -> dict[str, list[str]] | None:
    """The urlencoded body, or None when it is not that or too big."""
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return None
    try:
        return parse_qs(body.decode(), keep_blank_values=True, max_num_fields=300)
    except ValueError:
        return None


def _same_origin(request: Request) -> bool:
    """A browser form post carries Origin; if it names another site, refuse. (Absent: the form
    token and the SameSite cookie still stand.)"""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


def review_router(
    services: ReviewServices, limiter: tokens.FailureLimiter | None = None
) -> APIRouter:
    router = APIRouter()
    limits = limiter or tokens.FailureLimiter()

    def source(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def session(request: Request) -> tuple[tokens.Claims | None, str]:
        """(who, why not). A missing or expired cookie is not an attack; a forged one is counted."""
        cookie = request.cookies.get(COOKIE, "")
        if not cookie:
            return None, "none"
        moment = services.core.clock.now()
        verdict, claims = tokens.check(services.secret, cookie, tokens.SESSION, moment)
        if verdict is tokens.Verdict.INVALID:
            limits.record(source(request), moment)
        return claims, verdict.value

    @router.get("/review/enter", include_in_schema=False)
    def enter(request: Request, t: str = "", day: str = "") -> Response:
        """Trade a signed link for a session cookie, then go to the clean address."""
        moment = services.core.clock.now()
        if limits.blocked(source(request), moment):
            return _html(pages.too_many(), 429)
        verdict, _ = tokens.check(services.secret, t, tokens.LINK, moment)
        if verdict is not tokens.Verdict.OK:
            if verdict is tokens.Verdict.INVALID:
                limits.record(source(request), moment)
            return _html(pages.signed_out(verdict.value), 401)
        cookie = tokens.issue(services.secret, tokens.SESSION, moment, tokens.SESSION_TTL)
        target = f"/review?day={day}" if _valid_day(day) else "/review"
        response = RedirectResponse(target, status_code=303, headers=HEADERS)
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=int(tokens.SESSION_TTL.total_seconds()),
            path="/review",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/review", include_in_schema=False)
    async def page(request: Request, day: str = "") -> Response:
        moment = services.core.clock.now()
        if limits.blocked(source(request), moment):
            return _html(pages.too_many(), 429)
        claims, why = session(request)
        if claims is None:
            return _html(pages.signed_out(why), 401)
        return _html(await _render(services, claims, _day(day, _today(services))))

    @router.post("/review/decide", include_in_schema=False)
    async def decide(request: Request) -> Response:
        moment = services.core.clock.now()
        if limits.blocked(source(request), moment):
            return _html(pages.too_many(), 429)
        claims, why = session(request)
        if claims is None:
            return _html(pages.signed_out(why), 401)
        form = await _form(request)
        if form is None or not _same_origin(request):
            limits.record(source(request), moment)
            return _html(pages.refused("That request did not come from this page."), 400)
        supplied = (form.get("csrf") or [""])[0]
        if not tokens.csrf_ok(services.secret, claims, supplied):
            limits.record(source(request), moment)
            return _html(pages.refused("The form was out of date. Open the page again."), 403)
        action = (form.get("do") or [""])[0]
        ids = [i for i in form.get("v", []) if i]
        if action not in ("approve", "reject") or not ids:
            return _html(pages.refused("There was nothing to approve or reject."), 400)
        done = await run_in_threadpool(
            daily_review.decide,
            services.core,
            services.tenant_id,
            ids,
            approve=action == "approve",
        )
        day = _day((form.get("day") or [""])[0], _today(services))
        return _html(await _render(services, claims, day, done))

    return router


def _valid_day(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


async def _render(
    services: ReviewServices,
    claims: tokens.Claims,
    day: date,
    done: daily_review.Decided | None = None,
) -> str:
    view = await run_in_threadpool(
        daily_review.load_day, services.core, services.tenant_id, day, services.preview
    )
    csrf = tokens.csrf_token(services.secret, claims)
    return pages.review_page(view, _today(services).isoformat(), csrf, done)
