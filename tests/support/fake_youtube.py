"""An in-memory YouTube: it implements our own `YouTubeApi`, so publisher rules need no Google."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dk_publishing.adapters.platforms.youtube_api import UploadResult, VideoInfo
from dk_publishing.domain.errors import PublishingError
from tests.support import T0


class FakeYouTube:
    def __init__(self, uploaded_at: datetime | None = None, force_private: bool = False) -> None:
        self.uploaded_at = uploaded_at or T0 + timedelta(minutes=1)
        self.force_private = force_private  # an unaudited project: everything stays private
        self.videos: dict[str, dict[str, Any]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.fail_next: PublishingError | None = None
        self.lose_response_next = False
        self.list_lag = 0  # the next N listings hide the newest upload, as YouTube really does
        self._n = 0

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

    def upload(self, *, body: Mapping[str, Any], file_path: str) -> UploadResult:
        self.calls.append("upload")
        self._maybe_fail()
        self._n += 1
        vid = f"vid{self._n}"
        status = dict(body["status"])
        if self.force_private:
            status["privacyStatus"] = "private"
        self.videos[vid] = {
            "snippet": dict(body["snippet"]),
            "status": status,
            "at": self.uploaded_at,
        }
        self.uploads.append({"id": vid, "body": dict(body), "bytes": Path(file_path).read_bytes()})
        if self.lose_response_next:
            self.lose_response_next = False
            from dk_publishing.domain.errors import UnknownOutcome

            raise UnknownOutcome("no answer from YouTube: lost")
        return UploadResult(vid, status["privacyStatus"])

    def _info(self, vid: str, now: datetime | None = None) -> VideoInfo:
        v = self.videos[vid]
        privacy, due = v["status"]["privacyStatus"], v["status"].get("publishAt")
        if due and now is not None and not self.force_private:
            when = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if when <= now:
                privacy, due = "public", None  # YouTube made it public by itself
        return VideoInfo(
            video_id=vid,
            title=v["snippet"]["title"],
            privacy=privacy,
            published_at=v["at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            scheduled_for=due,
            processed=True,
        )

    now: datetime | None = None  # set by a test to let scheduled videos go public

    def video(self, video_id: str) -> VideoInfo | None:
        self.calls.append("video")
        self._maybe_fail()
        return self._info(video_id, self.now) if video_id in self.videos else None

    def recent_uploads(self, limit: int = 25) -> list[VideoInfo]:
        self.calls.append("recent_uploads")
        self._maybe_fail()
        ids = list(self.videos)
        if self.list_lag > 0:
            self.list_lag -= 1
            ids = ids[:-1]
        return [self._info(v, self.now) for v in reversed(ids)][:limit]

    def delete(self, video_id: str) -> None:
        self.calls.append("delete")
        self._maybe_fail()
        self.deleted.append(video_id)
        self.videos.pop(video_id, None)
