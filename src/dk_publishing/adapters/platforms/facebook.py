"""Publish to a Facebook Page through the Graph API.

This version publishes directly at the slot. Native scheduling (letting Facebook hold the post)
comes with the per-row `delivery` choice. A video is uploaded inside `publish`, so a large file
adds to the lateness; uploading ahead as a draft is a later optimisation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.application.ports import Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import PublishingError, Rejected
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

MAX_CAPTION = 63_206
MAX_VIDEO_BYTES = 1024**3  # one request; larger files need Meta's chunked upload
MAX_PHOTO_BYTES = 10 * 1024**2
VIDEO_TYPES = (".mp4", ".mov", ".m4v")
PHOTO_TYPES = (".jpg", ".jpeg", ".png")
FORMATS = ("post", "photo", "video")
# How far before the slot a matching post still counts as ours when asking "is it live?".
LOOKBACK = timedelta(minutes=15)


class FacebookPublisher:
    def __init__(self, *, page_id: str, capabilities: Capabilities, graph: GraphClient) -> None:
        self._page = page_id
        self._capabilities = capabilities
        self._graph = graph

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    # --- validate --------------------------------------------------------------------------

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        content = snapshot.content
        fmt = str(content.get("format") or "")
        caption = str(content.get("caption") or "")
        names = [str(m.get("name", "")) for m in content.get("media") or []]
        problems: list[Violation] = []

        if fmt == "reel":
            return [Violation("format", "Facebook reels are not supported yet. Use video.")]
        if fmt not in FORMATS:
            return [Violation("format", f"format must be one of: {', '.join(FORMATS)}.")]
        if len(caption) > MAX_CAPTION:
            problems.append(
                Violation(
                    "caption",
                    f"Caption is {len(caption):,} characters; Facebook allows {MAX_CAPTION:,}.",
                )
            )
        if fmt == "post":
            if not caption.strip():
                problems.append(Violation("caption", "A text post needs a caption."))
            if names:
                problems.append(
                    Violation("media", "A text post cannot have media; use format photo or video.")
                )
        else:
            problems.extend(_media_problems(fmt, names))
        if content.get("link") and fmt != "post":
            problems.append(Violation("link", "A link can only be attached to a text post."))
        return problems

    # --- prepare ---------------------------------------------------------------------------

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        """Check the files are really there and small enough. Posts nothing."""
        fmt = str(snapshot.content.get("format"))
        paths = [r.path for r in media]
        if fmt in ("photo", "video"):
            if not paths:
                raise Rejected("the media file was not downloaded, so there is nothing to upload")
            limit = MAX_VIDEO_BYTES if fmt == "video" else MAX_PHOTO_BYTES
            for path in paths:
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    raise Rejected(f"the prepared file {Path(path).name} is missing") from None
                if size > limit:
                    raise Rejected(
                        f"{Path(path).name} is {size / 1024**2:,.0f} MB; Facebook allows "
                        f"{limit / 1024**2:,.0f} MB in one upload"
                    )
        return {
            "kind": fmt,
            "page_id": self._page,
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "message": str(snapshot.content.get("caption") or ""),
            "link": snapshot.content.get("link") or None,
            "files": paths,
        }

    # --- publish ---------------------------------------------------------------------------

    def publish(self, handle: Handle) -> LivePost:
        kind, message = handle.get("kind"), str(handle.get("message") or "")
        files = list(handle.get("files") or [])
        page = handle.get("page_id") or self._page
        if kind == "post":
            data: dict[str, Any] = {"message": message}
            if handle.get("link"):
                data["link"] = handle["link"]
            result = self._graph.post(f"{page}/feed", data)
            return self._live(result["id"])
        if kind in ("photo", "video") and files:
            with open(files[0], "rb") as handle_file:
                if kind == "photo":
                    result = self._graph.post(
                        f"{page}/photos",
                        {"caption": message, "published": "true"},
                        file=("source", handle_file),
                    )
                    return self._live(result.get("post_id") or result["id"])
                result = self._graph.post(
                    f"{page}/videos",
                    {"description": message, "published": "true"},
                    file=("source", handle_file),
                    video=True,
                )
            return self._live(result["id"], video_page=page)
        raise Rejected("this prepared post is missing what it needs to be published")

    def _live(self, post_id: str, video_page: str | None = None) -> LivePost:
        """The post is already live here: nothing below may raise, or a published post would be
        reported as a failure."""
        fallback = (
            f"https://www.facebook.com/{video_page}/videos/{post_id}"
            if video_page
            else f"https://www.facebook.com/{post_id}"
        )
        try:
            link = self._graph.get(post_id, {"fields": "permalink_url"}).get("permalink_url")
        except PublishingError:
            return LivePost(str(post_id), fallback)
        if isinstance(link, str) and link:
            return LivePost(
                str(post_id), f"https://www.facebook.com{link}" if link.startswith("/") else link
            )
        return LivePost(str(post_id), fallback)

    # --- find_live -------------------------------------------------------------------------

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        """Is this post on the Page? None means confirmed not there. If Facebook cannot answer,
        this raises, so the variant is checked by hand instead of guessed at."""
        fmt = str(snapshot.content.get("format"))
        message = str(snapshot.content.get("caption") or "").strip()
        since = snapshot.publish_at - LOOKBACK
        edge, field = ("videos", "description") if fmt == "video" else ("feed", "message")
        if fmt == "photo":
            edge, field = "photos", "name"
        params = {"fields": f"id,{field},created_time,permalink_url", "limit": "25"}
        if edge == "photos":
            params["type"] = "uploaded"
        for item in self._graph.get(f"{self._page}/{edge}", params).get("data", []):
            if str(item.get(field) or "").strip() != message:
                continue
            if _created(item) >= since:
                return self._live_from(item)
        return None

    @staticmethod
    def _live_from(item: Mapping[str, Any]) -> LivePost:
        link = item.get("permalink_url")
        if isinstance(link, str) and link.startswith("/"):
            link = f"https://www.facebook.com{link}"
        return LivePost(str(item["id"]), link if isinstance(link, str) else None)


def _created(item: Mapping[str, Any]) -> datetime:
    return datetime.strptime(str(item["created_time"]), "%Y-%m-%dT%H:%M:%S%z")


def _media_problems(fmt: str, names: list[str]) -> list[Violation]:
    wanted, kinds = ("video", VIDEO_TYPES) if fmt == "video" else ("photo", PHOTO_TYPES)
    if not names:
        return [Violation("media", f"A {wanted} post needs a {wanted} file in the media column.")]
    if len(names) > 1:
        return [Violation("media", f"Only one {wanted} per post is supported so far.")]
    if not names[0].lower().endswith(kinds):
        return [Violation("media", f"'{names[0]}' is not a {wanted} file ({', '.join(kinds)}).")]
    return []
