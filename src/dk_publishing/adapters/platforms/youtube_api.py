"""A thin interface over the YouTube Data API, plus the real client and the consent flow.

The publisher talks only to `YouTubeApi`, so its rules are tested without Google. The real client
turns every Google failure into one of the five domain errors.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from dk_publishing.adapters.config.secrets_file import SecretsFile
from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",  # read status, and delete a video we scheduled
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024
QUOTA_WAIT = timedelta(hours=1)
QUOTA_REASONS = {
    "quotaExceeded",
    "rateLimitExceeded",
    "dailyLimitExceeded",
    "userRateLimitExceeded",
}
AUTH_REASONS = {"authError", "forbidden", "insufficientPermissions", "youtubeSignupRequired"}


@dataclass(frozen=True, slots=True)
class UploadResult:
    video_id: str
    privacy: str  # what YouTube actually applied (an unaudited project is forced to private)


@dataclass(frozen=True, slots=True)
class VideoInfo:
    video_id: str
    title: str
    privacy: str
    published_at: str | None  # RFC 3339
    scheduled_for: str | None  # status.publishAt, if the video is waiting to go public
    processed: bool


class YouTubeApi(Protocol):
    def upload(self, *, body: Mapping[str, Any], file_path: str) -> UploadResult:
        """Upload a video (resumable). A write: a lost answer is UnknownOutcome."""

    def video(self, video_id: str) -> VideoInfo | None:
        """One of our videos by id, or None if it does not exist."""

    def recent_uploads(self, limit: int = 25) -> list[VideoInfo]:
        """The channel's newest uploads, newest first."""

    def delete(self, video_id: str) -> None:
        """Delete a video. A video that is already gone counts as deleted."""


# --- the credentials file ---------------------------------------------------------------------


class YouTubeCredentials:
    """client_id, client_secret and the refresh token from one-time consent, in one file kept
    outside the repository. Read fresh on every call."""

    def __init__(self, path: Path | str) -> None:
        self._file = SecretsFile(path, what="YouTube credentials")
        self.path = self._file.path

    def load(self) -> dict[str, Any]:
        return self._file.read()

    def update(self, changes: Mapping[str, Any]) -> None:
        self._file.update(changes)

    def google_credentials(self) -> Credentials:
        data = self.load()
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not data.get(k)]
        if missing:
            raise AuthFailed(
                f"{self.path} is missing {', '.join(missing)}; run `dk connect youtube`"
            )
        return Credentials(  # type: ignore[no-untyped-call]
            token=None,
            refresh_token=data["refresh_token"],
            token_uri=TOKEN_URI,
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            scopes=list(SCOPES),
        )


def write_template(path: Path) -> bool:
    """Create the credentials file with owner-only permissions. False if it exists."""
    path = path.expanduser()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "_help": (
            "Keep this file OUTSIDE the git repository. Paste the OAuth client's id and secret "
            "(Google Cloud > APIs & Services > Credentials > Create OAuth client ID > Desktop "
            "app), then run `make connect-youtube`; it fills in refresh_token and channel_id."
        ),
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "channel_id": "",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(template, handle, indent=2)
        handle.write("\n")
    return True


# --- the real client ---------------------------------------------------------------------------


def map_google_error(exc: BaseException, *, write: bool) -> PublishingError:
    """Every way a Google call can fail, as one of the five outcomes. After a write that may
    have reached Google, trouble is UnknownOutcome (reconciled), never plain Retryable."""
    if isinstance(exc, RefreshError):
        return AuthFailed(
            "Google refused the saved login (the refresh token expired or was revoked); "
            "run `dk connect youtube` again"
        )
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 0) or 0)
        reason = _reason(exc)
        if reason in QUOTA_REASONS:
            return RateLimited(QUOTA_WAIT)
        if status == 401 or reason in AUTH_REASONS:
            return AuthFailed(
                f"YouTube refused the login ({reason or status}); run `dk connect youtube`"
            )
        if status >= 500:
            text = f"YouTube had a problem (HTTP {status})"
            return UnknownOutcome(text) if write else Retryable(text)
        return Rejected(f"YouTube refused it: {reason or status}: {_message(exc)}")
    if isinstance(exc, ConnectionRefusedError | socket.gaierror):
        return Retryable("could not reach YouTube")  # never left this machine
    text = f"no answer from YouTube: {type(exc).__name__}"
    return UnknownOutcome(text) if write else Retryable(text)


def _reason(exc: HttpError) -> str:
    try:
        return str(json.loads(exc.content.decode())["error"]["errors"][0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""


def _message(exc: HttpError) -> str:
    try:
        return str(json.loads(exc.content.decode())["error"]["message"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""


def _media(path: str) -> Any:
    return MediaFileUpload(path, chunksize=CHUNK, resumable=True)


class GoogleYouTubeApi:
    def __init__(
        self,
        service: Callable[[], Any],
        *,
        media: Callable[[str], Any] = _media,
    ) -> None:
        self._service = service
        self._media = media

    @classmethod
    def from_credentials(cls, credentials: YouTubeCredentials) -> GoogleYouTubeApi:
        return cls(
            lambda: build(
                "youtube", "v3", credentials=credentials.google_credentials(), cache_discovery=False
            )
        )

    def upload(self, *, body: Mapping[str, Any], file_path: str) -> UploadResult:
        try:
            request = (
                self._service()
                .videos()
                .insert(
                    part="snippet,status",
                    body=dict(body),
                    media_body=self._media(file_path),
                    notifySubscribers=False,
                )
            )
            response = None
            while response is None:
                _, response = request.next_chunk(num_retries=3)
        except PublishingError:
            raise
        except Exception as exc:
            raise map_google_error(exc, write=True) from None
        return UploadResult(
            str(response["id"]), str(response.get("status", {}).get("privacyStatus", ""))
        )

    def video(self, video_id: str) -> VideoInfo | None:
        try:
            reply = self._service().videos().list(part="snippet,status", id=video_id).execute()
        except Exception as exc:
            raise map_google_error(exc, write=False) from None
        items = reply.get("items") or []
        return _info(items[0]) if items else None

    def recent_uploads(self, limit: int = 25) -> list[VideoInfo]:
        try:
            service = self._service()
            channel = service.channels().list(part="contentDetails", mine=True).execute()
            uploads = channel["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            listing = (
                service.playlistItems()
                .list(part="snippet", playlistId=uploads, maxResults=limit)
                .execute()
            )
            ids = [i["snippet"]["resourceId"]["videoId"] for i in listing.get("items", [])]
            if not ids:
                return []
            details = service.videos().list(part="snippet,status", id=",".join(ids)).execute()
        except Exception as exc:
            raise map_google_error(exc, write=False) from None
        by_id = {i["id"]: _info(i) for i in details.get("items", [])}
        return [by_id[i] for i in ids if i in by_id]

    def channel(self) -> tuple[str, str]:
        """(id, title) of the channel the login belongs to."""
        try:
            reply = self._service().channels().list(part="snippet", mine=True).execute()
            item = reply["items"][0]
        except Exception as exc:
            raise map_google_error(exc, write=False) from None
        return str(item["id"]), str(item.get("snippet", {}).get("title", ""))

    def delete(self, video_id: str) -> None:
        try:
            self._service().videos().delete(id=video_id).execute()
        except HttpError as exc:
            if int(getattr(exc.resp, "status", 0) or 0) == 404 or _reason(exc) == "videoNotFound":
                return  # already gone
            raise map_google_error(exc, write=False) from None
        except Exception as exc:
            raise map_google_error(exc, write=False) from None


def _info(item: Mapping[str, Any]) -> VideoInfo:
    snippet, status = item.get("snippet", {}), item.get("status", {})
    return VideoInfo(
        video_id=str(item["id"]),
        title=str(snippet.get("title", "")),
        privacy=str(status.get("privacyStatus", "")),
        published_at=snippet.get("publishedAt"),
        scheduled_for=status.get("publishAt"),
        processed=status.get("uploadStatus") in ("processed", None),
    )


# --- one-time consent ----------------------------------------------------------------------------


def run_consent(client_id: str, client_secret: str) -> str:
    """Open the browser for the one-time consent and return the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=list(SCOPES),
    )
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise AuthFailed("Google returned no refresh token; revoke the app's access and try again")
    return str(credentials.refresh_token)
