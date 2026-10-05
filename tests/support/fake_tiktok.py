"""An in-memory TikTok that implements our own `TikTokApi`, so publisher rules need no network."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dk_publishing.adapters.platforms.tiktok_api import (
    CreatorInfo,
    PostStatus,
    UploadSlot,
)
from dk_publishing.domain.errors import PublishingError


class MemoryLog:
    """The publish record (and the assisted 'card sent' record): a set of keys, in order."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def was_sent(self, key: str) -> bool:
        return key in self.keys

    def mark_sent(self, key: str) -> None:
        if key not in self.keys:
            self.keys.append(key)

    def lookup(self, prefix: str) -> list[str]:
        return [k for k in self.keys if k.startswith(prefix)]


class FakeTikTok:
    def __init__(self, options: tuple[str, ...] = ("SELF_ONLY",)) -> None:
        self.options = options
        self.inits: list[dict[str, Any]] = []
        self.uploads: list[bytes] = []
        self.calls: list[str] = []
        self.posts: dict[str, dict[str, Any]] = {}
        self.fail_next: PublishingError | None = None
        self.lose_upload_answer = False
        self.final_status = "PUBLISH_COMPLETE"
        self.fail_reason = "file_format_check_failed"
        self.polls_before_done = 0
        self._n = 0

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

    def creator_info(self) -> CreatorInfo:
        self.calls.append("creator_info")
        self._maybe_fail()
        return CreatorInfo("dhakakacchi", "Dhaka Kacchi", self.options, False, False, False, 600)

    def init_video(
        self, *, post_info: Mapping[str, Any], size: int, chunk: int, chunks: int
    ) -> UploadSlot:
        self.calls.append("init_video")
        self._maybe_fail()
        self._n += 1
        pid = f"v_pub_file~v2-1.{self._n}"
        self.inits.append(
            {"post_info": dict(post_info), "size": size, "chunk": chunk, "chunks": chunks}
        )
        self.posts[pid] = {"uploaded": False, "polls": 0}
        return UploadSlot(pid, f"https://upload.example/{pid}")

    def upload(self, slot: UploadSlot, path: str, *, chunk: int) -> None:
        self.calls.append("upload")
        self._maybe_fail()
        self.uploads.append(Path(path).read_bytes())
        self.posts[slot.publish_id]["uploaded"] = True

    def status(self, publish_id: str) -> PostStatus:
        self.calls.append("status")
        self._maybe_fail()
        post = self.posts[publish_id]
        if not post["uploaded"]:
            return PostStatus("PROCESSING_UPLOAD", None, ())
        post["polls"] += 1
        if post["polls"] <= self.polls_before_done:
            return PostStatus("PROCESSING_DOWNLOAD", None, ())
        if self.final_status == "FAILED":
            return PostStatus("FAILED", self.fail_reason, ())
        return PostStatus(self.final_status, None, ())
