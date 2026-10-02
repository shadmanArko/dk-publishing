"""Publish to Threads through the Threads API.

Two steps, as Threads defines them: create a container (this is `prepare`, done ahead of the
slot so Threads has time to process media), then publish the container (this is `publish`).
Threads fetches images and videos from a public URL itself, so media posts need the public-media
link store; text posts need nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from dk_publishing.adapters.media.public import PublicMedia
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.application.ports import Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import PublishingError, Rejected, Retryable
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

HOST = "https://graph.threads.net"
DEFAULT_VERSION = "v1.0"
MAX_TEXT = 500
FORMATS = ("text", "image", "video")
IMAGE_TYPES = (".jpg", ".jpeg", ".png")
VIDEO_TYPES = (".mp4", ".mov")
REPLY_CONTROLS = ("everyone", "accounts_you_follow", "mentioned_only")
LOOKBACK = timedelta(minutes=15)


class ThreadsPublisher:
    def __init__(
        self,
        *,
        user_id: str,
        capabilities: Capabilities,
        graph: GraphClient,
        public: PublicMedia | None = None,
        sleep: Callable[[float], None] = time.sleep,
        ready_timeout: float = 120.0,
        poll_every: float = 5.0,
    ) -> None:
        self._user = user_id
        self._capabilities = capabilities
        self._graph = graph
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
        text = str(content.get("caption") or "")
        names = [str(m.get("name", "")) for m in content.get("media") or []]
        if fmt == "carousel":
            return [Violation("format", "Threads carousels are not supported yet.")]
        if fmt not in FORMATS:
            return [Violation("format", f"format must be one of: {', '.join(FORMATS)}.")]

        problems: list[Violation] = []
        used = _length(text)
        if used > MAX_TEXT:
            problems.append(
                Violation("caption", f"The text is {used} characters; Threads allows {MAX_TEXT}.")
            )
        control = str(content.get("reply_control") or "")
        if control and control not in REPLY_CONTROLS:
            problems.append(
                Violation(
                    "reply_control", f"reply_control must be one of: {', '.join(REPLY_CONTROLS)}."
                )
            )
        if fmt == "text":
            if not text.strip():
                problems.append(Violation("caption", "A text post needs text."))
            if names:
                problems.append(
                    Violation("media", "A text post cannot have media; use format image or video.")
                )
            return problems

        kinds = IMAGE_TYPES if fmt == "image" else VIDEO_TYPES
        if len(names) != 1:
            problems.append(
                Violation("media", f"A Threads {fmt} post needs exactly one {fmt} file.")
            )
        elif not names[0].lower().endswith(kinds):
            problems.append(
                Violation("media", f"'{names[0]}' is not a {fmt} file ({', '.join(kinds)}).")
            )
        if self._public is None:
            problems.append(
                Violation(
                    "media",
                    "Threads fetches media from a public web address, which only the server "
                    "provides (PUBLIC_MEDIA_BASE_URL). Use a text post from here.",
                )
            )
        return problems

    # --- prepare: create the container ------------------------------------------------------

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        """Create the container. Posts nothing; a repeat just makes another unused container."""
        fmt = str(snapshot.content.get("format"))
        data: dict[str, Any] = {"media_type": fmt.upper()}
        text = str(snapshot.content.get("caption") or "")
        if text:
            data["text"] = text
        if snapshot.content.get("reply_control"):
            data["reply_control"] = snapshot.content["reply_control"]

        urls: list[str] = []
        if fmt in ("image", "video"):
            if self._public is None or not media:
                raise Rejected("this media post has no file to publish or no public address for it")
            url = self._public.expose(media[0])
            urls.append(url)
            data["image_url" if fmt == "image" else "video_url"] = url
        try:
            container = self._graph.post(f"{self._user}/threads", data)["id"]
            if urls:
                self._wait_until_ready(container)
        except PublishingError:
            for url in urls:  # a container that never became usable must not leave a public link
                self._public.revoke(url) if self._public else None
            raise
        return {
            "kind": fmt,
            "user_id": self._user,
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "container_id": container,
            "public_urls": urls,
        }

    def _wait_until_ready(self, container_id: str) -> None:
        waited = 0.0
        while True:
            status = self._status(container_id)
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise Rejected(f"Threads could not process the media (status {status})")
            if waited >= self._ready_timeout:
                raise Retryable("Threads is still processing the media")
            self._sleep(self._poll_every)
            waited += self._poll_every

    def _status(self, container_id: str) -> str:
        item = self._graph.get(container_id, {"fields": "status,error_message"})
        return str(item.get("status") or "")

    # --- publish ----------------------------------------------------------------------------

    def publish(self, handle: Handle) -> LivePost:
        container = handle.get("container_id")
        if not container:
            raise Rejected("this prepared post has no container to publish")
        user = str(handle.get("user_id") or self._user)
        if handle.get("public_urls"):
            # A read before the write: if the media is not ready nothing has been sent yet,
            # so waiting and trying again is safe.
            status = self._status(str(container))
            if status in ("ERROR", "EXPIRED"):
                raise Rejected(f"the prepared media is no longer usable (status {status})")
            if status != "FINISHED":
                raise Retryable("Threads has not finished processing the media yet")
        result = self._graph.post(f"{user}/threads_publish", {"creation_id": container})
        for url in handle.get("public_urls") or []:
            if self._public:
                self._public.revoke(str(url))
        return self._live(str(result["id"]))

    def _live(self, thread_id: str) -> LivePost:
        """Already live: nothing here may raise, or a published post would be reported failed."""
        try:
            link = self._graph.get(thread_id, {"fields": "permalink"}).get("permalink")
        except PublishingError:
            return LivePost(thread_id, None)
        return LivePost(thread_id, link if isinstance(link, str) else None)

    # --- find_live --------------------------------------------------------------------------

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        """Is this post on the account? None means confirmed not there; if Threads cannot
        answer, this raises so the variant is checked by hand instead of guessed at."""
        text = str(snapshot.content.get("caption") or "").strip()
        since = snapshot.publish_at - LOOKBACK
        params = {"fields": "id,text,timestamp,permalink", "limit": "25"}
        for item in self._graph.get(f"{self._user}/threads", params).get("data", []):
            if str(item.get("text") or "").strip() == text and _stamp(item) >= since:
                link = item.get("permalink")
                return LivePost(str(item["id"]), link if isinstance(link, str) else None)
        if (
            handle
            and handle.get("container_id")
            and self._status(str(handle["container_id"])) == "PUBLISHED"
        ):
            return LivePost(str(handle["container_id"]), None)  # live, but not yet in the list
        return None


def _stamp(item: dict[str, Any]) -> datetime:
    return datetime.strptime(str(item["timestamp"]), "%Y-%m-%dT%H:%M:%S%z")


def _length(text: str) -> int:
    """Threads counts characters, and emoji count as their UTF-8 bytes."""
    return sum(len(c.encode("utf-8")) if ord(c) > 0xFFFF else 1 for c in text)
