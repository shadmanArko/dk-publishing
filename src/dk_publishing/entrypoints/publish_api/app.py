"""The HTTP API: a thin shell over the same use cases the Sheet sync uses.

Nothing here decides anything about publishing. It authenticates, turns JSON into the use cases'
requests, and turns their answers (and every failure) into JSON. Errors follow RFC 9457
(`application/problem+json`) and carry a `problems` list that says what to fix.

No `from __future__ import annotations` here: FastAPI reads parameter annotations at runtime.
"""

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from dk_publishing.application.ports import (
    ApiKey,
    IntakeRejected,
    StoredFile,
    WebhookDelivery,
    WebhookEndpoint,
)
from dk_publishing.application.services import ApiServices
from dk_publishing.application.use_cases import api_keys
from dk_publishing.application.use_cases import webhooks as hooks
from dk_publishing.application.use_cases.api_batch import MAX_BATCH, BatchItem, submit_batch
from dk_publishing.application.use_cases.api_media import register_media
from dk_publishing.application.use_cases.api_posts import (
    ApiError,
    PostRequest,
    PostView,
    Problem,
    VariantRequest,
    approve_post,
    cancel_post,
    get_post,
    list_posts,
    platform_guides,
    request_example,
    submit_post,
    validate_post,
)
from dk_publishing.application.use_cases.media_retention import upload_allowance
from dk_publishing.application.use_cases.publishing_spec import build_spec, spec_slice
from dk_publishing.application.use_cases.publishing_status import calendar, overview
from dk_publishing.domain.guide import FieldGuide, FormatGuide, PlatformGuide

PROBLEM = "application/problem+json"
STATUS = {
    "invalid": 422,
    "conflict": 409,
    "not_found": 404,
    "forbidden": 403,
    "quota_exceeded": 413,
    "storage_full": 507,
}
UPLOAD_REFUSALS = ("quota_exceeded", "storage_full")  # IntakeRejected codes with their own status
TITLE = {
    "invalid": "The request has problems",
    "conflict": "Conflict",
    "not_found": "Not found",
    "forbidden": "Forbidden",
    "unauthorized": "Authentication required",
    "quota_exceeded": "Upload quota used",
    "storage_full": "Server storage is full",
}
GUIDE_FILE = Path(__file__).with_name("llms.txt")


class _Unauthorized(Exception):
    pass


JSON_BODY_PATHS = ("/v1/posts", "/v1/posts/validate", "/v1/posts/batch", "/v1/webhooks")
GUESSED_TYPES = (b"", b"application/x-www-form-urlencoded", b"text/plain")


class _JsonByDefault:
    """`curl -d` sends form-urlencoded and hand-written clients often send nothing. The post and
    webhook endpoints only ever take JSON, so treat those labels as JSON. Media uploads are
    untouched (their body is raw bytes), and a body that is not JSON still gets the usual 422."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"].rstrip("/") in JSON_BODY_PATHS
        ):
            headers = scope["headers"]
            kind = next((v for k, v in headers if k == b"content-type"), b"")
            if kind.split(b";")[0].strip().lower() in GUESSED_TYPES:
                scope = {
                    **scope,
                    "headers": [(k, v) for k, v in headers if k != b"content-type"]
                    + [(b"content-type", b"application/json")],
                }
        await self.app(scope, receive, send)


# --- request bodies ---------------------------------------------------------------------------


class VariantIn(BaseModel):
    """One post on one platform. Anything not listed here is refused, so a typo is caught."""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(description="A platform key from GET /v1/platforms.")
    format: str = Field(description="A format of that platform, e.g. reel. See GET /v1/platforms.")
    slot: str | None = Field(
        default=None,
        description="When to publish: ISO 8601 with a UTC offset, e.g. 2026-10-12T19:30:00+02:00.",
    )
    caption: str | None = Field(
        default=None, description="The text of the post, if the format has one."
    )
    media: list[str] = Field(
        default_factory=list, description="Ids returned by POST /v1/media, in order."
    )
    account: str | None = Field(
        default=None, description="Which connected account. Optional when only one is connected."
    )
    options: dict[str, Any] = Field(
        default_factory=dict, description="The format's extra fields, e.g. title or privacy_level."
    )


class PostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_key: str = Field(
        description="Your own unique id for this post. Sending the same post_key and content again "
        "is safe: you get the existing post back, never a second one."
    )
    variants: list[VariantIn]
    label: str | None = Field(default=None, description="An internal name; never published.")
    approve: bool = Field(
        default=False,
        description="False (default): create drafts that a person approves. True needs a key with "
        "approve rights.",
    )


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    posts: list[PostIn] = Field(description=f"1 to {MAX_BATCH} posts; each is handled on its own.")
    dry_run: bool = Field(
        default=False,
        description="True: check every post and create nothing (linked files are downloaded so "
        "they can be checked).",
    )


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Where to send messages. https only, a public host.")
    events: list[str] = Field(
        default_factory=list,
        description="Which events you want, e.g. post.published. Leave empty for all of them.",
    )


class MediaFromUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="A public https link to the file.")
    filename: str | None = Field(
        default=None, description="Keep the right extension, e.g. reel.mp4."
    )


# --- JSON helpers -----------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, (), [], {})}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _clean_dict(value: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _clean(value))


def problem_json(problem: Problem) -> dict[str, Any]:
    return cast(dict[str, Any], _clean(dataclasses.asdict(problem)))


def post_json(view: PostView) -> dict[str, Any]:
    data = dataclasses.asdict(view)
    for variant in data["variants"]:
        variant["slot"] = variant["slot"].isoformat()
    return cast(dict[str, Any], _clean(data))


def batch_json(items: list[BatchItem], dry_run: bool, spec_version: str) -> dict[str, Any]:
    """One answer per post, in the order sent. The same shape over HTTP and MCP."""
    results = [
        _clean_dict(
            {
                "post_key": i.post_key,
                "ok": i.ok,
                "replayed": i.replayed or None,
                "post": post_json(i.post) if i.post else None,
                "error": i.error,
                "message": i.message,
                "problems": [problem_json(p) for p in i.problems],
                "examples": dict(i.examples) if i.examples else None,
            }
        )
        for i in items
    ]
    return {
        "dry_run": dry_run,
        "spec_version": spec_version,
        "ok": sum(i.ok for i in items),
        "failed": sum(not i.ok for i in items),
        "results": results,
    }


def endpoint_json(e: WebhookEndpoint) -> dict[str, Any]:
    return _clean_dict(
        {
            "id": e.id,
            "url": e.url,
            "events": list(e.events) or "all",
            "active": e.active,
            "disabled_reason": e.disabled_reason,
            "created_at": e.created_at.isoformat(),
        }
    )


def delivery_json(d: WebhookDelivery) -> dict[str, Any]:
    return _clean_dict(
        {
            "event_id": d.event_id,
            "type": d.event_type,
            "status": d.status,
            "attempts": d.attempts,
            "http_status": d.last_status,
            "error": d.last_error,
            "next_attempt_at": d.next_attempt_at.isoformat()
            if d.status == "pending" and d.next_attempt_at
            else None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
        }
    )


def _field_json(f: FieldGuide) -> dict[str, Any]:
    return cast(dict[str, Any], _clean(dataclasses.asdict(f)))


def _format_json(f: FormatGuide) -> dict[str, Any]:
    return _clean_dict(
        {
            "summary": f.summary,
            "media": dataclasses.asdict(f.media),
            "caption": _field_json(f.caption) if f.caption else "not supported",
            "options": {o.name: _field_json(o) for o in f.fields},
            "example": request_example("(this platform)", f.example),
        }
    )


def guide_json(guide: PlatformGuide, mode: str, accounts: list[str]) -> dict[str, Any]:
    meaning = {
        "live": "Posts are really published.",
        "assisted": "The post is handed to a person on Telegram to publish by hand.",
        "dry_run": "Rehearsal only: nothing is posted to the real platform.",
    }
    return _clean_dict(
        {
            "platform": guide.platform,
            "summary": guide.summary,
            "mode": mode,
            "mode_meaning": meaning.get(mode, mode),
            "accounts": accounts,
            "formats": {f.name: _format_json(f) for f in guide.formats},
            "notes": guide.notes,
        }
    )


def problem_response(
    kind: str,
    detail: str,
    problems: list[Problem] | None = None,
    examples: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    status = {**STATUS, "unauthorized": 401}.get(kind, 400)
    body: dict[str, Any] = {
        "type": f"/v1/problems/{kind}",
        "title": TITLE.get(kind, "Bad request"),
        "status": status,
        "detail": detail,
    }
    if problems:
        body["problems"] = [problem_json(p) for p in problems]
    if examples:
        body["examples"] = examples
    return JSONResponse(body, status_code=status, media_type=PROBLEM, headers=headers)


# --- the app ----------------------------------------------------------------------------------


def create_app(api: ApiServices, mcp_app: Any = None) -> FastAPI:
    """`mcp_app`: the MCP endpoint's ASGI app (with its lifespan), served at /mcp/ when given."""
    app = FastAPI(
        lifespan=mcp_app.lifespan if mcp_app is not None else None,
        title="DK Publishing API",
        version="1.0",
        description=(
            "Schedule social posts. Read GET /v1/platforms to learn what each platform accepts, "
            "POST /v1/posts/validate to check a post without creating it, then POST /v1/posts. "
            "Every error says what to fix. Plain-text guide for agents: GET /llms.txt."
        ),
    )

    app.add_middleware(_JsonByDefault)

    def caller(request: Request) -> ApiKey:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        key = api_keys.authenticate(api.core, token.strip()) if scheme.lower() == "bearer" else None
        if key is None:
            raise _Unauthorized
        return key

    def needs(scope: str) -> Callable[[ApiKey], ApiKey]:
        def check(key: Annotated[ApiKey, Depends(caller)]) -> ApiKey:
            if not api_keys.allows(key, scope):
                raise ApiError(
                    "forbidden", f"This key cannot do that: it needs the '{scope}' scope."
                )
            return key

        return check

    Reader = Annotated[ApiKey, Depends(needs(api_keys.READ))]
    Drafter = Annotated[ApiKey, Depends(needs(api_keys.DRAFT))]
    Approver = Annotated[ApiKey, Depends(needs(api_keys.APPROVE))]

    # errors ------------------------------------------------------------------------------

    @app.exception_handler(ApiError)
    def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return problem_response(exc.kind, exc.message, list(exc.problems), exc.examples)

    @app.exception_handler(_Unauthorized)
    def _unauthorized(_: Request, __: _Unauthorized) -> JSONResponse:
        return problem_response(
            "unauthorized",
            "Send your key as 'Authorization: Bearer dkp_...'. Keys are issued by the owner.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(RequestValidationError)
    def _bad_body(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            Problem(
                ".".join(str(p) for p in e["loc"] if p != "body") or "body",
                "unknown_field" if e["type"] == "extra_forbidden" else "invalid",
                str(e["msg"]),
            )
            for e in exc.errors()
        ]
        return problem_response(
            "invalid", "The request body is not what the API expects. See /openapi.json.", problems
        )

    @app.exception_handler(StarletteHTTPException)
    def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        kind = "not_found" if exc.status_code == 404 else "invalid"
        response = problem_response(kind, str(exc.detail))
        response.status_code = exc.status_code
        return response

    # open --------------------------------------------------------------------------------

    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/llms.txt", include_in_schema=False)
    def llms() -> Response:
        text = GUIDE_FILE.read_text() if GUIDE_FILE.exists() else "DK Publishing API: see /docs."
        return PlainTextResponse(text)

    # read --------------------------------------------------------------------------------

    @app.get("/v1/platforms", summary="What every platform accepts")
    def platforms(_: Reader) -> dict[str, Any]:
        return {
            "platforms": [
                guide_json(g, mode, accounts) for g, mode, accounts in platform_guides(api)
            ]
        }

    @app.get("/v1/platforms/{name}", summary="One platform's rules")
    def one_platform(name: str, _: Reader) -> dict[str, Any]:
        for guide, mode, accounts in platform_guides(api):
            if guide.platform == name:
                return guide_json(guide, mode, accounts)
        raise ApiError("not_found", f"No platform '{name}' here. See GET /v1/platforms.")

    @app.get("/v1/spec", summary="The publishing contract: everything a producer must know")
    def spec(
        request: Request,
        response: Response,
        _: Reader,
        platform: str | None = None,
        fmt: Annotated[str | None, Query(alias="format")] = None,
    ) -> Any:
        """Every rule a post must meet, per platform and format, with an example and a JSON Schema
        for each. `spec_version` changes when anything in it does; it is also the ETag, so send
        If-None-Match to get 304 when nothing changed."""
        whole = build_spec(api)
        etag = f'"{whole["spec_version"]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return spec_slice(whole, platform, fmt)

    @app.get("/v1/overview", summary="Where publishing stands: pipeline, published, problems")
    def status_overview(_: Reader, days_back: int = 7, days_ahead: int = 14) -> dict[str, Any]:
        """Every post, the Google Sheet's too: what waits, what is coming, what went out in the
        last `days_back` days, what needs a look, and the last slot that is planned."""
        return overview(api, days_back, days_ahead)

    @app.get("/v1/calendar", summary="Every post by slot; state=published is the history")
    def status_calendar(
        _: Reader,
        state: str | None = None,
        platform: str | None = None,
        source: str | None = None,
        start: Annotated[str | None, Query(alias="from")] = None,
        end: Annotated[str | None, Query(alias="to")] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Posts from the Sheet and the API, narrowed by state, platform, source (sheet, api)
        and dates (ISO 8601 with an offset). Newest first when state=published."""
        return calendar(
            api, state=state, platform=platform, source=source, start=start, end=end, limit=limit
        )

    @app.get("/v1/posts", summary="Posts made through the API, newest first")
    def posts(_: Reader, limit: int = 50) -> dict[str, Any]:
        return {"posts": [post_json(p) for p in list_posts(api, max(1, min(limit, 200)))]}

    @app.get("/v1/posts/{post_key}", summary="Where a post stands, per platform")
    def one_post(post_key: str, _: Reader) -> dict[str, Any]:
        return post_json(get_post(api, post_key))

    # write -------------------------------------------------------------------------------

    @app.post("/v1/media", status_code=201, summary="Add a file (bytes, or a public https link)")
    async def add_media(
        request: Request, key: Drafter, filename: str | None = None
    ) -> dict[str, Any]:
        """Send the file itself as the request body with `?filename=reel.mp4`, or send JSON
        `{"url": "https://...", "filename": "reel.mp4"}` and the server downloads it."""
        try:
            limit = await run_in_threadpool(upload_allowance, api, key.name)
            if request.headers.get("content-type", "").startswith("application/json"):
                body = MediaFromUrl.model_validate(await request.json())
                stored = await run_in_threadpool(api.intake.fetch, body.url, body.filename, limit)
            else:
                if not filename:
                    raise IntakeRejected(
                        "filename_required",
                        "Add ?filename=reel.mp4 (the extension says what it is).",
                    )
                stored = await _receive(api, request, filename, limit)
        except IntakeRejected as exc:
            kind = exc.code if exc.code in UPLOAD_REFUSALS else "invalid"
            raise ApiError(kind, exc.message, [Problem("media", exc.code, exc.message)]) from None
        media = await run_in_threadpool(register_media, api, stored, created_by=key.name)
        return {
            "media_id": media.id,
            "filename": media.filename,
            "size": media.size,
            "sha256": media.sha256,
        }

    @app.post("/v1/posts/validate", summary="Check a post without creating anything")
    def validate(body: PostIn, _: Drafter) -> dict[str, Any]:
        checked = validate_post(api, _request(body))
        return {
            "valid": not checked.problems,
            **_clean_dict(
                {
                    "problems": [problem_json(p) for p in checked.problems],
                    "examples": checked.examples,
                }
            ),
        }

    @app.post(
        "/v1/posts", status_code=201, summary="Create a post (a draft unless approve is true)"
    )
    def submit(body: PostIn, response: Response, key: Drafter) -> dict[str, Any]:
        result = submit_post(
            api,
            _request(body),
            actor_name=key.name,
            may_approve=api_keys.allows(key, api_keys.APPROVE),
        )
        if result.replayed:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return post_json(result.post)

    @app.post("/v1/posts/batch", summary="Check or create up to 20 posts in one call (drafts)")
    def batch(body: BatchIn, key: Drafter) -> dict[str, Any]:
        """Each post is checked and created like POST /v1/posts, on its own: one bad post never
        stops the others, and sending the same batch again is safe. Always drafts. A media entry
        may be a media_id or a public https link (downloaded once)."""
        items = submit_batch(
            api, [_request(p) for p in body.posts], actor_name=key.name, dry_run=body.dry_run
        )
        return batch_json(items, body.dry_run, build_spec(api)["spec_version"])

    @app.post("/v1/posts/{post_key}/approve", summary="Approve a draft post")
    def approve(post_key: str, key: Approver) -> dict[str, Any]:
        return post_json(approve_post(api, post_key, actor_name=key.name))

    @app.delete("/v1/posts/{post_key}", summary="Cancel everything that has not gone out")
    def cancel(post_key: str, key: Drafter) -> dict[str, Any]:
        return post_json(cancel_post(api, post_key, actor_name=key.name))

    # webhooks ----------------------------------------------------------------------------

    @app.post("/v1/webhooks", status_code=201, summary="Get messages when your posts change state")
    def add_webhook(body: WebhookIn, key: Drafter) -> dict[str, Any]:
        endpoint, secret = hooks.register_endpoint(api, key, body.url, body.events)
        return {
            **endpoint_json(endpoint),
            "secret": secret,
            "note": "Save the secret now: it signs every message and is not shown again.",
        }

    @app.get("/v1/webhooks", summary="Your webhooks (the secret is never shown again)")
    def list_webhooks(key: Reader) -> dict[str, Any]:
        return {"webhooks": [endpoint_json(e) for e in hooks.list_endpoints(api, key)]}

    @app.delete("/v1/webhooks/{endpoint_id}", summary="Stop sending to this webhook")
    def delete_webhook(endpoint_id: str, key: Drafter) -> dict[str, Any]:
        hooks.delete_endpoint(api, key, endpoint_id)
        return {"deleted": True}

    @app.get("/v1/webhooks/{endpoint_id}/deliveries", summary="The latest messages and their fate")
    def webhook_deliveries(endpoint_id: str, key: Reader) -> dict[str, Any]:
        found = hooks.list_deliveries(api, key, endpoint_id)
        return {"deliveries": [delivery_json(d) for d in found]}

    @app.post("/v1/webhooks/{endpoint_id}/ping", summary="Send a test message now")
    def ping_webhook(endpoint_id: str, key: Drafter) -> dict[str, Any]:
        result = hooks.ping(api, key, endpoint_id)
        return _clean_dict(
            {"delivered": result.ok, "http_status": result.http_status, "error": result.error}
        )

    return app


async def _receive(
    api: ApiServices, request: Request, filename: str, limit: int | None
) -> StoredFile:
    writer = api.intake.begin(filename, limit)
    try:
        async for chunk in request.stream():
            writer.write(chunk)
        return writer.finish()
    except BaseException:
        writer.abort()
        raise


def _request(body: PostIn) -> PostRequest:
    return PostRequest(
        post_key=body.post_key,
        label=body.label,
        approve=body.approve,
        variants=tuple(
            VariantRequest(
                platform=v.platform,
                format=v.format,
                caption=v.caption,
                media=tuple(v.media),
                slot=v.slot,
                account=v.account,
                options=v.options,
            )
            for v in body.variants
        ),
    )
