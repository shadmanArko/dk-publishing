"""Publish to a YouTube channel through the YouTube Data API.

Two ways to get a video out, chosen per row by `delivery`:
- direct: this system uploads at the slot and the video goes live as soon as it is uploaded.
- native: the video is uploaded now as private with a `publishAt` time, and YouTube makes it
  public by itself at that time, even if this system is off.

YouTube locks every upload from an API project that has not passed Google's compliance audit to
private, whatever the row asks for. The upload still succeeds, so such a video is reported as
published; it simply stays private until the audit is done.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dk_publishing.adapters.platforms.youtube_api import VideoInfo, YouTubeApi
from dk_publishing.application.ports import Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import Rejected
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500
VIDEO_TYPES = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm")
VISIBILITIES = ("public", "unlisted", "private")
CATEGORIES = {
    "People & Blogs": "22",
    "Entertainment": "24",
    "Howto & Style": "26",
    "Education": "27",
    "News & Politics": "25",
    "Travel & Events": "19",
}
LOOKBACK = timedelta(minutes=15)
WATCH = "https://www.youtube.com/watch?v="


class YouTubePublisher:
    def __init__(self, *, api: YouTubeApi, capabilities: Capabilities) -> None:
        self._api = api
        self._capabilities = capabilities

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    # --- validate --------------------------------------------------------------------------

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        c = snapshot.content
        title = str(c.get("title") or "").strip()
        names = [str(m.get("name", "")) for m in c.get("media") or []]
        problems: list[Violation] = []

        if not title:
            problems.append(Violation("title", "A YouTube video needs a title."))
        elif len(title) > MAX_TITLE:
            problems.append(
                Violation(
                    "title", f"The title is {len(title)} characters; YouTube allows {MAX_TITLE}."
                )
            )
        if "<" in title or ">" in title:
            problems.append(Violation("title", "A title cannot contain < or >."))
        description = _description(c)
        if len(description.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            problems.append(
                Violation(
                    "description",
                    f"The description is longer than {MAX_DESCRIPTION_BYTES:,} bytes.",
                )
            )
        if len(", ".join(_tags(c))) > MAX_TAGS_CHARS:
            problems.append(
                Violation("tags", f"The tags together are over {MAX_TAGS_CHARS} characters.")
            )
        category = c.get("category")
        if category and category not in CATEGORIES:
            problems.append(
                Violation("category", f"category must be one of: {', '.join(CATEGORIES)}.")
            )
        visibility = c.get("visibility_after_publish")
        if visibility and visibility not in VISIBILITIES:
            problems.append(
                Violation("visibility_after_publish", f"must be one of: {', '.join(VISIBILITIES)}.")
            )
        if c.get("made_for_kids") not in ("yes", "no"):
            problems.append(
                Violation("made_for_kids", "YouTube needs an explicit yes or no for every video.")
            )
        delivery = str(c.get("delivery") or "direct")
        if delivery == "native" and visibility == "private":
            problems.append(
                Violation(
                    "visibility_after_publish",
                    "A natively scheduled video becomes public at the slot; private would make "
                    "the schedule pointless. Use public or unlisted.",
                )
            )
        if len(names) != 1:
            problems.append(Violation("media", "A YouTube video needs exactly one video file."))
        elif not names[0].lower().endswith(VIDEO_TYPES):
            problems.append(
                Violation("media", f"'{names[0]}' is not a video file ({', '.join(VIDEO_TYPES)}).")
            )
        return problems

    # --- prepare ---------------------------------------------------------------------------

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        """Check the file is there. Uploads nothing: a direct upload happens at the slot."""
        if not media:
            raise Rejected("the video file was not downloaded, so there is nothing to upload")
        path = media[0].path
        if not Path(path).is_file():
            raise Rejected(f"the prepared file {Path(path).name} is missing")
        return {
            "kind": "video",
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "file": path,
            "body": _body(snapshot.content, publish_at=None),
        }

    # --- publish (direct) -------------------------------------------------------------------

    def publish(self, handle: Handle) -> LivePost:
        file_path, body = handle.get("file"), handle.get("body")
        if not file_path or not isinstance(body, dict):
            raise Rejected("this prepared video is missing what it needs to be uploaded")
        result = self._api.upload(body=body, file_path=str(file_path))
        return LivePost(result.video_id, WATCH + result.video_id)

    # --- native scheduling ------------------------------------------------------------------

    def schedule(
        self, snapshot: VariantSnapshot, media: Sequence[Rendition], at: datetime
    ) -> Handle:
        """Upload now as private with a publish time; YouTube makes it public at `at`."""
        handle = self.prepare(snapshot, media)
        body = _body(snapshot.content, publish_at=at)
        result = self._api.upload(body=body, file_path=str(handle["file"]))
        return {
            **handle,
            "body": body,
            "scheduled_id": result.video_id,
            "scheduled_for": at.astimezone(UTC).isoformat(),
        }

    def cancel(self, handle: Handle) -> None:
        """Delete the scheduled video. One that is already gone counts as cancelled."""
        video_id = handle.get("scheduled_id")
        if not video_id:
            raise Rejected("nothing was scheduled for this variant")
        self._api.delete(str(video_id))

    # --- find_live --------------------------------------------------------------------------

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        """Is this video on the channel? None means confirmed not there; if YouTube cannot
        answer, this raises so the variant is checked by hand instead of guessed at."""
        if handle and handle.get("scheduled_id"):
            return self._scheduled_is_live(str(handle["scheduled_id"]))
        title = str(snapshot.content.get("title") or "").strip()
        since = snapshot.publish_at - LOOKBACK
        for video in self._api.recent_uploads():
            if video.title.strip() == title and _when(video.published_at) >= since:
                return LivePost(video.video_id, WATCH + video.video_id)
        return None

    def _scheduled_is_live(self, video_id: str) -> LivePost | None:
        video = self._api.video(video_id)
        if video is None:
            raise Rejected("the scheduled video no longer exists on the channel")
        if _is_public(video):
            return LivePost(video_id, WATCH + video_id)
        return None


def _is_public(video: VideoInfo) -> bool:
    return video.privacy in ("public", "unlisted") and video.scheduled_for is None


def _description(content: Mapping[str, Any]) -> str:
    return str(content.get("description") or content.get("caption") or "")


def _tags(content: Mapping[str, Any]) -> list[str]:
    return [t.strip() for t in str(content.get("tags") or "").split(",") if t.strip()]


def _body(content: Mapping[str, Any], *, publish_at: datetime | None) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": str(content.get("title") or "").strip(),
        "description": _description(content),
        "categoryId": CATEGORIES.get(str(content.get("category") or ""), "22"),
    }
    if _tags(content):
        snippet["tags"] = _tags(content)
    status: dict[str, Any] = {
        "selfDeclaredMadeForKids": content.get("made_for_kids") == "yes",
        "privacyStatus": str(content.get("visibility_after_publish") or "public"),
    }
    if publish_at is not None:
        # YouTube publishes a private video at `publishAt`; it must be an RFC 3339 UTC time.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"snippet": snippet, "status": status}


def _when(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
