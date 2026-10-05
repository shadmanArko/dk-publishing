"""Post to TikTok through the Content Posting API (Direct Post).

An app TikTok has not audited can only post privately, and only to a private account; that is
enforced by TikTok, and the answer is passed on in plain words. The sequence for one post:

1. `prepare` (before the slot, posts nothing): read the account's allowed privacy levels and check
   that the row's choice is one of them.
2. `publish`: start the post, write down its publish id, upload the file, then wait for TikTok's
   answer. The id is written BEFORE the upload so a crash can be settled afterwards.
3. `find_live`: for a variant that has a written-down publish id, ask TikTok what became of it.
   Without one nothing was ever started, which is a safe "not live".
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from dk_publishing.adapters.platforms.tiktok import TikTokRules
from dk_publishing.adapters.platforms.tiktok_api import PRIVACY_LEVELS, PostStatus, TikTokApi
from dk_publishing.application.ports import Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import Rejected, Retryable, UnknownOutcome
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

MB = 1024 * 1024
SMALL = 10 * MB  # up to this size the file goes up in one piece
CHUNK = 10 * MB  # TikTok allows 5-64 MB per chunk; the last one absorbs the remainder
DONE = "PUBLISH_COMPLETE"
FAILED = "FAILED"


class PublishLog(Protocol):
    def mark_sent(self, key: str) -> None: ...

    def lookup(self, prefix: str) -> list[str]: ...


def _prefix(tenant_id: str, variant_id: str) -> str:
    return f"tiktok:{tenant_id}:{variant_id}:"


def chunking(size: int) -> tuple[int, int]:
    """(chunk size, chunk count) as TikTok defines them: the last chunk takes the remainder."""
    chunk = size if size <= SMALL else CHUNK
    return chunk, max(1, size // chunk)


class TikTokDirectPublisher:
    def __init__(
        self,
        *,
        api: TikTokApi,
        capabilities: Capabilities,
        log: PublishLog,
        sleep: Callable[[float], None] = time.sleep,
        wait: float = 240.0,
        poll_every: float = 5.0,
    ) -> None:
        self._api = api
        self._capabilities = capabilities
        self._log = log
        self._sleep = sleep
        self._wait = wait
        self._poll_every = poll_every
        self._rules = TikTokRules()

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        return self._rules.validate(snapshot)

    # --- prepare ----------------------------------------------------------------------------

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        if not media:
            raise Rejected("the media file was not downloaded, so there is nothing to upload")
        path = media[0].path
        if not os.path.isfile(path):
            raise Rejected(f"{os.path.basename(path)} is missing, so it cannot be uploaded")
        c = snapshot.content
        level = PRIVACY_LEVELS[str(c.get("privacy_level"))]
        creator = self._api.creator_info()
        if level not in creator.privacy_options:
            offered = ", ".join(creator.privacy_options) or "none"
            raise Rejected(
                f"TikTok does not allow '{c.get('privacy_level')}' for this account right now. "
                f"It allows: {offered}. Change privacy_level in the row"
            )
        post_info: dict[str, Any] = {
            "title": str(c.get("caption") or ""),
            "privacy_level": level,
            "disable_comment": not c.get("allow_comments") or creator.comment_disabled,
            "disable_duet": not c.get("allow_duet") or creator.duet_disabled,
            "disable_stitch": not c.get("allow_stitch") or creator.stitch_disabled,
            "brand_organic_toggle": bool(c.get("commercial_disclosure")),
            "brand_content_toggle": False,
        }
        return {
            "kind": "direct",
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "file": path,
            "size": os.path.getsize(path),
            "username": creator.username,
            "post_info": post_info,
        }

    # --- publish ----------------------------------------------------------------------------

    def publish(self, handle: Handle) -> LivePost:
        path, post_info = handle.get("file"), handle.get("post_info")
        variant, tenant = handle.get("variant_id"), handle.get("tenant_id")
        if not (path and post_info and variant and tenant):
            raise Rejected("this prepared post is missing what it needs to be uploaded")
        size = os.path.getsize(str(path))
        chunk, chunks = chunking(size)
        slot = self._api.init_video(post_info=post_info, size=size, chunk=chunk, chunks=chunks)
        # Written down before the upload: from here on, a crash is settled by asking TikTok.
        self._log.mark_sent(f"{_prefix(str(tenant), str(variant))}{slot.publish_id}")
        self._api.upload(slot, str(path), chunk=chunk)
        status = self._wait_for_answer(slot.publish_id)
        if status.status == FAILED:
            raise Rejected(
                f"TikTok could not publish it: {status.fail_reason or 'no reason given'}"
            )
        return self._live(slot.publish_id, status, str(handle.get("username") or ""))

    def _wait_for_answer(self, publish_id: str) -> PostStatus:
        waited = 0.0
        while True:
            status = self._api.status(publish_id)
            if status.status in (DONE, FAILED):
                return status
            if waited >= self._wait:
                raise UnknownOutcome("TikTok is still processing the video; checking later")
            self._sleep(self._poll_every)
            waited += self._poll_every

    # --- find_live --------------------------------------------------------------------------

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        prefix = _prefix(snapshot.tenant_id, snapshot.variant_id)
        ids = [key[len(prefix) :] for key in self._log.lookup(prefix)]
        if not ids:
            return None  # nothing was ever started
        username = str((handle or {}).get("username") or "")
        processing = False
        for publish_id in ids:
            status = self._api.status(publish_id)
            if status.status == DONE:
                return self._live(publish_id, status, username)
            if status.status != FAILED:
                processing = True
        if processing:
            raise Retryable("TikTok is still processing the video; cannot tell yet")
        return None

    @staticmethod
    def _live(publish_id: str, status: PostStatus, username: str) -> LivePost:
        if status.post_ids and username:
            return LivePost(
                publish_id, f"https://www.tiktok.com/@{username}/video/{status.post_ids[0]}"
            )
        return LivePost(publish_id, None)  # private or still being moderated: no public address
