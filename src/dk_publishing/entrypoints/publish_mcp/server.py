"""The MCP shell (ADR 0024): the same use cases as the HTTP API, for any agent that speaks MCP.

Like the HTTP API it decides nothing: it authenticates with the same API keys, calls the same use
cases and returns the same JSON. A `read` key sees the four read tools; a `draft` key also sees
`submit_posts` and `cancel_post`. There is no approve tool, whatever the key may do over HTTP: a
person approves in the Calendar. Bytes never travel through MCP: a file is uploaded with
POST /v1/media (or given as a public https link), so an agent's context only ever holds ids.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from pydantic import Field

from dk_publishing.application.ports import ApiKey
from dk_publishing.application.services import ApiServices
from dk_publishing.application.use_cases import api_keys
from dk_publishing.application.use_cases.api_batch import MAX_BATCH, submit_batch
from dk_publishing.application.use_cases.api_posts import ApiError
from dk_publishing.application.use_cases.api_posts import cancel_post as cancel_post_use_case
from dk_publishing.application.use_cases.api_posts import get_post as get_post_use_case
from dk_publishing.application.use_cases.publishing_spec import build_spec, spec_slice
from dk_publishing.application.use_cases.publishing_status import calendar, overview
from dk_publishing.entrypoints.publish_api.app import (
    GUIDE_FILE,
    BatchIn,
    _request,
    batch_json,
    post_json,
)

log = logging.getLogger("dk_publishing.mcp")

READ_TOOLS = frozenset({"publishing_overview", "list_posts", "get_post", "publishing_spec"})
DRAFT_TOOLS = frozenset({"submit_posts", "cancel_post"})
MAX_ANSWER = 32_000  # characters; a list longer than this is cut and says so

INSTRUCTIONS = """Social publishing for one business: where posts stand, the rules every post must
meet, and handing over posts as drafts a person approves.
- Questions about status: publishing_overview first (one call), list_posts for details.
- Before MAKING content: publishing_spec(platform, format) gives the exact rules and an example.
- Handing over content you already have: call submit_posts directly. It creates only the posts
  that pass; each refused one comes back with what to fix and a valid example (and is not
  created). Files: POST /v1/media ids or public https links. Every post waits for approval.
- Never resend a post whose state is `checking`. To change a post: cancel it, submit a new one."""

PROMPT = "publish_posts"


class KeyVerifier(TokenVerifier):
    """The same API keys as the HTTP API (Authorization: Bearer dkp_...)."""

    def __init__(self, api: ApiServices) -> None:
        super().__init__()
        self.api = api

    async def verify_token(self, token: str) -> AccessToken | None:
        key = await anyio.to_thread.run_sync(api_keys.authenticate, self.api.core, token)
        if key is None:
            return None
        return AccessToken(
            token="",  # never kept
            client_id=key.name,
            scopes=list(key.scopes),
            claims={"key_id": key.id, "tenant_id": key.tenant_id},
        )


def caller() -> ApiKey | None:
    """The key behind this request, or None (no token: in-process use only)."""
    try:
        token = get_access_token()
    except RuntimeError:
        return None
    if token is None:
        return None
    claims = token.claims or {}
    return ApiKey(
        str(claims.get("key_id", "")),
        str(claims.get("tenant_id", "")),
        token.client_id,
        tuple(token.scopes),
    )


def _right(key: ApiKey | None, tool: str) -> bool:
    if key is None:
        return False
    return api_keys.allows(key, api_keys.DRAFT if tool in DRAFT_TOOLS else api_keys.READ)


class RightGate(Middleware):
    """A key sees and may call only what its right covers; a switched-off tool is gone for all.
    Hidden from every list AND refused on call (a list alone blocks nothing)."""

    def __init__(self, who: Callable[[], ApiKey | None], disabled: frozenset[str]) -> None:
        self.who, self.disabled = who, disabled

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        key = self.who()
        tools = await call_next(context)
        return [t for t in tools if t.name not in self.disabled and _right(key, t.name)]

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        name = context.message.name
        if name in self.disabled:
            raise ToolError(f"[switched_off] {name} is switched off on this server.")
        if not _right(self.who(), name):
            raise ToolError(f"[forbidden] {name} needs a key with the 'draft' right.")
        return await call_next(context)


class CallLog(Middleware):
    """One line per tool call: tool, key, outcome, time. Arguments only as a hash; never a token."""

    def __init__(self, who: Callable[[], ApiKey | None]) -> None:
        self.who = who

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        started, status = time.monotonic(), "ok"
        try:
            return await call_next(context)
        except Exception:
            status = "error"
            raise
        finally:
            args = json.dumps(context.message.arguments or {}, sort_keys=True, default=str)
            key = self.who()
            log.info(
                "mcp tool=%s key=%s status=%s ms=%d args=%s",
                context.message.name,
                key.name if key else "-",
                status,
                (time.monotonic() - started) * 1000,
                hashlib.sha256(args.encode()).hexdigest()[:12],
            )


def _fail(exc: ApiError) -> ToolError:
    """An API refusal as a tool error the model can act on: code, what is wrong, what to fix."""
    lines = [f"[{exc.kind}] {exc.message}"]
    for p in exc.problems[:10]:
        allowed = f" Allowed: {', '.join(p.allowed)}." if p.allowed else ""
        lines.append(f"- {p.path}: {p.code}: {p.message}{allowed}")
    return ToolError("\n".join(lines))


def _small(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Keep an answer under MAX_ANSWER by cutting its longest list, and say so."""
    while len(json.dumps(data, default=str)) > MAX_ANSWER and len(data.get(key) or []) > 1:
        data = {**data, key: data[key][: len(data[key]) // 2], "cut": True}
    return data


def build_mcp(
    api: ApiServices,
    *,
    disabled: frozenset[str] = frozenset(),
    who: Callable[[], ApiKey | None] = caller,
    auth: bool = True,
) -> FastMCP:
    """The MCP server over `api`. `who` and `auth` are replaceable for in-process tests only."""

    def key_name() -> str:
        key = who()
        if key is None:
            raise ToolError("[unauthorized] Send your key as 'Authorization: Bearer dkp_...'.")
        return key.name

    async def run(fn: Callable[[], Any]) -> Any:
        try:
            return await anyio.to_thread.run_sync(fn)
        except ApiError as exc:
            raise _fail(exc) from None

    def client_id(_: MiddlewareContext[Any]) -> str:
        key = who()
        return key.name if key else "anonymous"

    mcp = FastMCP(
        "dk_publish",
        instructions=INSTRUCTIONS,
        auth=KeyVerifier(api) if auth else None,
        mask_error_details=True,
        middleware=[
            CallLog(who),
            RightGate(who, disabled),
            RateLimitingMiddleware(
                max_requests_per_second=5, burst_capacity=20, get_client_id=client_id
            ),
        ],
    )
    read = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(annotations=read)
    async def publishing_overview(
        days_back: Annotated[int, Field(ge=0, le=90, description="Look back this many days.")] = 7,
        days_ahead: Annotated[
            int, Field(ge=0, le=366, description="Look ahead this many days.")
        ] = 14,
    ) -> dict[str, Any]:
        """Use when: asked where publishing stands. One call answers how many posts wait for
        approval or are scheduled (per platform), what went out recently, what failed and why, the
        next slots, and the last planned slot (when the plan runs out). Covers every post: the
        Google Sheet's and the API's."""
        return await run(lambda: overview(api, days_back, days_ahead))  # type: ignore[no-any-return]

    @mcp.tool(annotations=read)
    async def list_posts(
        state: Annotated[
            str | None,
            Field(
                description="awaiting_approval, scheduled, publishing, checking, published, "
                "failed, invalid, cancelled or expired. published = the history, newest first."
            ),
        ] = None,
        platform: Annotated[
            str | None, Field(description="One platform key (see publishing_spec).")
        ] = None,
        source: Annotated[str | None, Field(description="sheet (made by a person) or api.")] = None,
        start: Annotated[
            str | None, Field(description="From this time, ISO 8601 with offset.")
        ] = None,
        end: Annotated[
            str | None, Field(description="Until this time, ISO 8601 with offset.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """Use when: you need the posts themselves, by slot: what went out where (with live links),
        what is coming, what failed. Each row: post_key, platform, account, format, state, slot,
        source, a text preview, live_url, error."""
        found = await run(
            lambda: calendar(
                api,
                state=state,
                platform=platform,
                source=source,
                start=start,
                end=end,
                limit=limit,
            )
        )
        return _small(found, "posts")

    @mcp.tool(annotations=read)
    async def get_post(post_key: str) -> dict[str, Any]:
        """Use when: you need one post's state on each platform, its live links and any error."""
        return post_json(await run(lambda: get_post_use_case(api, post_key)))

    @mcp.tool(annotations=read)
    async def publishing_spec(
        platform: Annotated[str | None, Field(description="Narrow to one platform.")] = None,
        format: Annotated[
            str | None, Field(description="Narrow to one format (needs platform).")
        ] = None,
    ) -> dict[str, Any]:
        """Use when: before making or submitting content. The exact rules a post must meet: files
        (count, types, size), caption and option limits, required options, accounts, timing, how
        it is published, a valid example and a JSON Schema. Narrow it to what you are making;
        spec_version changes when any rule does."""
        return await run(lambda: spec_slice(build_spec(api), platform, format))  # type: ignore[no-any-return]

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
    async def submit_posts(
        posts: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                max_length=MAX_BATCH,
                description="Posts as POST /v1/posts takes "
                "them: post_key, variants[{platform, format, slot, caption, media, options}].",
            ),
        ],
        dry_run: Annotated[bool, Field(description="True: check only, create nothing.")] = False,
    ) -> dict[str, Any]:
        """Use when: handing over posts. Creates up to 20 posts in one call, each on its own: only
        valid posts are created, each as a draft a person approves (dry_run=true: check only).
        media: ids from POST /v1/media, or public https links. Same post_key + same content =
        the same post (safe to resend). Each result: ok, the post's state, or problems with a
        valid example."""
        name = key_name()
        try:
            body = BatchIn.model_validate({"posts": posts, "dry_run": dry_run})
        except ValueError as exc:
            raise ToolError(f"[invalid] {str(exc)[:600]}") from None
        items = await run(
            lambda: submit_batch(
                api, [_request(p) for p in body.posts], actor_name=name, dry_run=dry_run
            )
        )
        version = await run(lambda: build_spec(api)["spec_version"])
        return batch_json(items, dry_run, version)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    async def cancel_post(post_key: str) -> dict[str, Any]:
        """Use when: a post this key submitted must not go out. Cancels everything not yet
        published. Posts made in the Google Sheet cannot be cancelled here."""
        name = key_name()
        return post_json(await run(lambda: cancel_post_use_case(api, post_key, actor_name=name)))

    @mcp.resource("dkp://spec", mime_type="application/json")
    def spec_resource() -> str:
        """The whole publishing contract (GET /v1/spec)."""
        return json.dumps(build_spec(api), ensure_ascii=False)

    @mcp.resource("dkp://guide", mime_type="text/plain")
    def guide_resource() -> str:
        """The plain-text guide for agents (GET /llms.txt)."""
        return Path(GUIDE_FILE).read_text() if Path(GUIDE_FILE).exists() else ""

    @mcp.prompt(name=PROMPT)
    def publish_posts(platform: str, what: str = "this week's posts") -> str:
        """How to hand over posts correctly, step by step."""
        return (
            f"Hand over {what} for {platform}.\n"
            f"1. Call publishing_spec(platform='{platform}') and keep to its rules and example.\n"
            "2. Files are uploaded by code with POST /v1/media (or are public https links); use "
            "their ids. Never put file contents in a message.\n"
            "3. Call submit_posts(posts). Only valid posts are created; fix each refused one\n"
            "   from its problems and example, and send just those again (same post_key is safe).\n"
            "4. Report each post_key with its state. Never resend a post that is `checking`."
        )

    return mcp
