"""Publish to an Instagram professional account through the Instagram Graph API (Facebook Login).

Two steps, as Instagram defines them: create a media container (`prepare`, ahead of the slot so
Instagram can process it), then publish the container (`publish`).

- A photo is fetched by Instagram from a public URL, so it needs the public-media link store (the
  server provides one; a laptop does not).
- A carousel (2-10 photos) is one container per photo plus one that holds them; a story is one photo
  or video and carries no caption. Both are fetched from public links.
- A reel is fetched the same way when a public-media store is configured (`video_url`). Without
  one it is uploaded straight from the local file with Instagram's resumable upload. The link is
  preferred because the resumable endpoint answered HTTP 500 "unknown error" for every file when
  tested against a real app (2026-10-02), while a link needs nothing from that endpoint.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dk_publishing.adapters.media.public import PublicMedia
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.application.ports import Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import PublishingError, RateLimited, Rejected, Retryable
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

UPLOAD_HOST = "https://rupload.facebook.com"
MAX_CAPTION = 2200
MAX_HASHTAGS = 30
FORMATS = ("feed", "carousel", "reel", "story")
REEL_TYPES = (".mp4", ".mov")
PHOTO_TYPES = (".jpg", ".jpeg")  # Instagram accepts JPEG only
STORY_TYPES = (*PHOTO_TYPES, *REEL_TYPES)  # a story is one photo or one short video
CAROUSEL_SIZE = (2, 10)
LOOKBACK = timedelta(minutes=15)


class InstagramPublisher:
    def __init__(
        self,
        *,
        account_id: str,
        capabilities: Capabilities,
        graph: GraphClient,
        version: str,
        public: PublicMedia | None = None,
        sleep: Callable[[float], None] = time.sleep,
        ready_timeout: float = 300.0,
        poll_every: float = 10.0,
    ) -> None:
        self._account = account_id
        self._capabilities = capabilities
        self._graph = graph
        self._version = version
        self._public = public
        self._sleep = sleep
        self._ready_timeout = ready_timeout
        self._poll_every = poll_every

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    # --- validate --------------------------------------------------------------------------

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        content = snapshot.content
        fmt = str(content.get("format") or "")
        caption = str(content.get("caption") or "")
        names = [str(m.get("name", "")) for m in content.get("media") or []]
        if fmt not in FORMATS:
            return [Violation("format", f"format must be one of: {', '.join(FORMATS)}.")]

        problems: list[Violation] = []
        if len(caption) > MAX_CAPTION:
            problems.append(
                Violation(
                    "caption",
                    f"Caption is {len(caption):,} characters; Instagram allows {MAX_CAPTION:,}.",
                )
            )
        if caption.count("#") > MAX_HASHTAGS:
            problems.append(
                Violation("caption", f"A caption can have at most {MAX_HASHTAGS} hashtags.")
            )
        problems.extend(_media_problems(fmt, names))
        if fmt != "reel" and self._public is None:
            problems.append(
                Violation(
                    "media",
                    "Instagram fetches photos, carousels and stories from a public web address, "
                    "which only the server provides (PUBLIC_MEDIA_BASE_URL). Use a reel from here.",
                )
            )
        cover = content.get("cover_at_s")
        if cover is not None and (not isinstance(cover, int | float) or cover < 0):
            problems.append(Violation("cover_at_s", "cover_at_s must be zero or more seconds."))
        return problems

    # --- prepare: create (and fill) the container -----------------------------------------

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        """Create the container, upload the media, wait until Instagram has processed it. Posts
        nothing; a repeat just makes another unused container (they expire after 24 hours)."""
        fmt = str(snapshot.content.get("format"))
        if not media:
            raise Rejected("the media file was not downloaded, so there is nothing to upload")
        self._check_quota()
        caption = str(snapshot.content.get("caption") or "")
        urls: list[str] = []
        try:
            if fmt == "carousel":
                container = self._carousel(media, caption, urls)
            else:
                container = self._single(fmt, snapshot, media[0], caption, urls)
        except PublishingError:
            for url in urls:  # a container that never became usable must not leave a public link
                if self._public:
                    self._public.revoke(url)
            raise
        return {
            "kind": fmt,
            "account_id": self._account,
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "container_id": container,
            "public_urls": urls,
        }

    def _single(
        self, fmt: str, snapshot: VariantSnapshot, file: Rendition, caption: str, urls: list[str]
    ) -> str:
        """A reel, a photo or a story: one container made from one file."""
        by_link = self._public is not None
        data: dict[str, Any] = {}
        if fmt != "story":
            data["caption"] = caption  # a story carries no caption on Instagram
        if fmt == "reel":
            data["media_type"] = "REELS"
            if not by_link:
                data["upload_type"] = "resumable"
            data["share_to_feed"] = "true" if snapshot.content.get("share_to_feed") else "false"
            cover = snapshot.content.get("cover_at_s")
            if cover is not None:
                data["thumb_offset"] = str(int(float(cover) * 1000))
        elif self._public is None:
            raise Rejected("this post has no public address to be fetched from")
        if fmt == "story":
            data["media_type"] = "STORIES"
        if self._public is not None:
            url = self._public.expose(file)
            urls.append(url)
            video = fmt == "reel" or (fmt == "story" and file.path.lower().endswith(REEL_TYPES))
            data["video_url" if video else "image_url"] = url
        created = self._graph.post(f"{self._account}/media", data)
        container = str(created["id"])
        if fmt == "reel" and not by_link:
            self._upload(created, container, file)
        self._wait_until_ready(container)
        return container

    def _carousel(self, media: Sequence[Rendition], caption: str, urls: list[str]) -> str:
        """One container per photo, then one container that holds them all."""
        if self._public is None:
            raise Rejected("this carousel has no public address to be fetched from")
        children: list[str] = []
        for file in media:
            url = self._public.expose(file)
            urls.append(url)
            child = self._graph.post(
                f"{self._account}/media", {"image_url": url, "is_carousel_item": "true"}
            )
            children.append(str(child["id"]))
        for child_id in children:
            self._wait_until_ready(child_id)
        created = self._graph.post(
            f"{self._account}/media",
            {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption},
        )
        container = str(created["id"])
        self._wait_until_ready(container)
        return container

    def _upload(self, created: dict[str, Any], container: str, file: Rendition) -> None:
        uri = created.get("uri") or f"{UPLOAD_HOST}/ig-api-upload/{self._version}/{container}"
        size = Path(file.path).stat().st_size
        with open(file.path, "rb") as handle:
            self._graph.upload(str(uri), handle, size=size)

    def _check_quota(self) -> None:
        """Instagram allows a fixed number of API posts per 24 hours. Best effort: if the
        allowance cannot be read, carry on; the publish call enforces it anyway."""
        try:
            data = (
                self._graph.get(
                    f"{self._account}/content_publishing_limit", {"fields": "quota_usage,config"}
                ).get("data")
                or []
            )
            used, total = int(data[0]["quota_usage"]), int(data[0]["config"]["quota_total"])
        except (PublishingError, LookupError, TypeError, ValueError):
            return
        if used >= total:
            raise RateLimited(timedelta(hours=1))

    def _wait_until_ready(self, container_id: str) -> None:
        waited = 0.0
        while True:
            status = self._status(container_id)
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise Rejected(f"Instagram could not process the media (status {status})")
            if waited >= self._ready_timeout:
                raise Retryable("Instagram is still processing the media")
            self._sleep(self._poll_every)
            waited += self._poll_every

    def _status(self, container_id: str) -> str:
        return str(
            self._graph.get(container_id, {"fields": "status_code"}).get("status_code") or ""
        )

    # --- publish ----------------------------------------------------------------------------

    def publish(self, handle: Handle) -> LivePost:
        container = handle.get("container_id")
        if not container:
            raise Rejected("this prepared post has no container to publish")
        account = str(handle.get("account_id") or self._account)
        # A read before the write: if the container is not ready nothing has been sent yet, so
        # waiting and trying again is safe.
        status = self._status(str(container))
        if status in ("ERROR", "EXPIRED"):
            raise Rejected(f"the prepared media is no longer usable (status {status})")
        if status != "FINISHED":
            raise Retryable("Instagram has not finished processing the media yet")

        result = self._graph.post(f"{account}/media_publish", {"creation_id": container})
        for url in handle.get("public_urls") or []:
            if self._public:
                self._public.revoke(str(url))
        return self._live(str(result["id"]))

    def _live(self, media_id: str) -> LivePost:
        """Already live: nothing here may raise, or a published post would be reported failed."""
        try:
            link = self._graph.get(media_id, {"fields": "permalink"}).get("permalink")
        except PublishingError:
            return LivePost(media_id, None)
        return LivePost(media_id, link if isinstance(link, str) else None)

    # --- find_live --------------------------------------------------------------------------

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        """Is this post on the account? None means confirmed not there; if Instagram cannot
        answer, this raises so the variant is checked by hand instead of guessed at."""
        caption = str(snapshot.content.get("caption") or "").strip()
        since = snapshot.publish_at - LOOKBACK
        params = {"fields": "id,caption,permalink,timestamp", "limit": "25"}
        story = str(snapshot.content.get("format")) == "story"  # no caption to recognise it by
        for item in (
            [] if story else self._graph.get(f"{self._account}/media", params).get("data", [])
        ):
            if str(item.get("caption") or "").strip() == caption and _stamp(item) >= since:
                link = item.get("permalink")
                return LivePost(str(item["id"]), link if isinstance(link, str) else None)
        container = handle.get("container_id") if handle else None
        if container and self._status(str(container)) == "PUBLISHED":
            return LivePost(str(container), None)  # live, but not in the list yet
        return None


def _media_problems(fmt: str, names: list[str]) -> list[Violation]:
    if fmt == "carousel":
        low, high = CAROUSEL_SIZE
        if not low <= len(names) <= high:
            return [
                Violation("media", f"A carousel needs {low} to {high} photos, not {len(names)}.")
            ]
        bad = [n for n in names if not n.lower().endswith(PHOTO_TYPES)]
        return [
            Violation("media", f"'{n}' is not a photo ({', '.join(PHOTO_TYPES)}).") for n in bad
        ]
    wanted, kinds = {
        "reel": ("reel", REEL_TYPES),
        "story": ("story", STORY_TYPES),
    }.get(fmt, ("photo", PHOTO_TYPES))
    if len(names) != 1:
        return [Violation("media", f"An Instagram {wanted} needs exactly one file.")]
    if not names[0].lower().endswith(kinds):
        return [Violation("media", f"'{names[0]}' is not a {wanted} file ({', '.join(kinds)}).")]
    return []


def _stamp(item: dict[str, Any]) -> datetime:
    return datetime.strptime(str(item["timestamp"]), "%Y-%m-%dT%H:%M:%S%z")
