"""Publish to an Instagram professional account through the Instagram Graph API (Facebook Login).

Two steps, as Instagram defines them: create a media container (`prepare`, ahead of the slot so
Instagram can process it), then publish the container (`publish`).

- A reel is uploaded straight from the local file with Instagram's resumable upload, so it needs no
  public web address.
- A photo is fetched by Instagram from a public URL, so it needs the public-media link store (the
  server provides one; a laptop does not).
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
FORMATS = ("feed", "reel")
REEL_TYPES = (".mp4", ".mov")
PHOTO_TYPES = (".jpg", ".jpeg")  # Instagram accepts JPEG only
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
        if fmt == "carousel":
            return [Violation("format", "Instagram carousels are not supported yet.")]
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
        wanted, kinds = ("reel", REEL_TYPES) if fmt == "reel" else ("photo", PHOTO_TYPES)
        if len(names) != 1:
            problems.append(Violation("media", f"An Instagram {wanted} needs exactly one file."))
        elif not names[0].lower().endswith(kinds):
            problems.append(
                Violation("media", f"'{names[0]}' is not a {wanted} file ({', '.join(kinds)}).")
            )
        if fmt == "feed" and self._public is None:
            problems.append(
                Violation(
                    "media",
                    "Instagram fetches photos from a public web address, which only the server "
                    "provides (PUBLIC_MEDIA_BASE_URL). Use a reel from here.",
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
        data: dict[str, Any] = {"caption": str(snapshot.content.get("caption") or "")}
        urls: list[str] = []

        if fmt == "reel":
            data.update({"media_type": "REELS", "upload_type": "resumable"})
            data["share_to_feed"] = "true" if snapshot.content.get("share_to_feed") else "false"
            cover = snapshot.content.get("cover_at_s")
            if cover is not None:
                data["thumb_offset"] = str(int(float(cover) * 1000))
        else:
            if self._public is None:
                raise Rejected("this photo has no public address to be fetched from")
            url = self._public.expose(media[0])
            urls.append(url)
            data["image_url"] = url

        try:
            created = self._graph.post(f"{self._account}/media", data)
            container = str(created["id"])
            if fmt == "reel":
                self._upload(created, container, media[0])
            self._wait_until_ready(container)
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
        for item in self._graph.get(f"{self._account}/media", params).get("data", []):
            if str(item.get("caption") or "").strip() == caption and _stamp(item) >= since:
                link = item.get("permalink")
                return LivePost(str(item["id"]), link if isinstance(link, str) else None)
        container = handle.get("container_id") if handle else None
        if container and self._status(str(container)) == "PUBLISHED":
            return LivePost(str(container), None)  # live, but not in the list yet
        return None


def _stamp(item: dict[str, Any]) -> datetime:
    return datetime.strptime(str(item["timestamp"]), "%Y-%m-%dT%H:%M:%S%z")
