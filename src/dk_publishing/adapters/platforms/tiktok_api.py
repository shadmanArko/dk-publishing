"""TikTok's Content Posting API and Login Kit, as plain HTTP calls.

Every answer is turned into the five domain outcomes. A write whose answer is lost is
`UnknownOutcome`; a read can simply be retried. The access token lasts 24 hours and is renewed from
the refresh token (valid a year) a little before it ends; the new tokens are written back to
`dk.json`. Nothing here logs a URL, a token or a request body.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.secrets_file import SecretsFile
from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)

API = "https://open.tiktokapis.com"
AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.publish"
RENEW_BEFORE = timedelta(minutes=10)
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
UPLOAD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
RATE_WAIT = timedelta(hours=1)

PRIVACY_LEVELS = {
    "public": "PUBLIC_TO_EVERYONE",
    "friends": "MUTUAL_FOLLOW_FRIENDS",
    "followers": "FOLLOWER_OF_CREATOR",
    "only_me": "SELF_ONLY",
}
_AUTH_CODES = {"access_token_invalid", "scope_not_authorized", "token_expired", "invalid_token"}
_RATE_CODES = {
    "rate_limit_exceeded",
    "spam_risk_too_many_posts",
    "spam_risk_too_many_pending_share",
}
_SERVER_CODES = {"internal_error", "service_unavailable", "timeout"}
_UNAUDITED = "unaudited_client_can_only_post_to_private_accounts"


@dataclass(frozen=True, slots=True)
class CreatorInfo:
    username: str
    nickname: str
    privacy_options: tuple[str, ...]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_duration_s: int


@dataclass(frozen=True, slots=True)
class PostStatus:
    status: str  # PROCESSING_UPLOAD, PROCESSING_DOWNLOAD, PUBLISH_COMPLETE, FAILED, ...
    fail_reason: str | None
    post_ids: tuple[str, ...]  # only once TikTok has published and moderated it


@dataclass(frozen=True, slots=True)
class UploadSlot:
    publish_id: str
    upload_url: str


class TikTokApi(Protocol):
    def creator_info(self) -> CreatorInfo: ...

    def init_video(
        self, *, post_info: Mapping[str, Any], size: int, chunk: int, chunks: int
    ) -> UploadSlot:
        """Start a post. A write: a lost answer is UnknownOutcome."""

    def upload(self, slot: UploadSlot, path: str, *, chunk: int) -> None:
        """Send the file in order. A failure before the last chunk leaves nothing posted."""

    def status(self, publish_id: str) -> PostStatus: ...


# --- the credentials, kept inside dk.json --------------------------------------------------------


class TikTokCredentials:
    def __init__(self, ref: Path | str) -> None:
        self._file = SecretsFile(ref, what="TikTok credentials")
        self.path = self._file.path

    def load(self) -> dict[str, Any]:
        return self._file.read()

    def update(self, changes: Mapping[str, Any]) -> None:
        self._file.update(changes)

    def app(self) -> tuple[str, str]:
        data = self.load()
        key, secret = str(data.get("client_key") or ""), str(data.get("client_secret") or "")
        if not key or not secret:
            raise ConfigError(f"fill in tiktok.client_key and client_secret in {self.path}")
        return key, secret


def authorize_url(client_key: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE}?{query}"


def _when(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _token_call(form: Mapping[str, str], transport: httpx.BaseTransport | None) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.post(f"{API}/v2/oauth/token/", data=dict(form))
    except httpx.HTTPError as exc:
        raise Retryable(f"could not reach TikTok ({type(exc).__name__})") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "access_token" not in body:
        reason = str(body.get("error_description") or body.get("error") or response.reason_phrase)
        raise AuthFailed(f"TikTok refused the login ({reason[:160]}); run `dk connect tiktok`")
    return dict(body)


def _stored(body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", ""),
        "access_expires_at": (
            now + timedelta(seconds=int(body.get("expires_in", 86400)))
        ).isoformat(),
        "refresh_expires_at": (
            now + timedelta(seconds=int(body.get("refresh_expires_in", 31536000)))
        ).isoformat(),
        "open_id": body.get("open_id", ""),
    }


def exchange_code(
    credentials: TikTokCredentials,
    code: str,
    redirect_uri: str,
    *,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Trade the one-time code from the sign-in for tokens and store them."""
    key, secret = credentials.app()
    body = _token_call(
        {
            "client_key": key,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        transport,
    )
    stored = _stored(body, now or datetime.now(UTC))
    credentials.update(stored)
    return stored


class TikTokTokens:
    """The current access token, renewed shortly before it ends."""

    def __init__(
        self,
        credentials: TikTokCredentials,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._clock = clock

    def token(self) -> str:
        data = self._credentials.load()
        access = str(data.get("access_token") or "")
        now = self._clock()
        expires = _when(data.get("access_expires_at"))
        if access and expires and expires - now > RENEW_BEFORE:
            return access
        refresh = str(data.get("refresh_token") or "")
        if not refresh:
            raise AuthFailed("no TikTok login yet; run `dk connect tiktok`")
        refresh_ends = _when(data.get("refresh_expires_at"))
        if refresh_ends and refresh_ends <= now:
            raise AuthFailed("the TikTok login expired; run `dk connect tiktok` again")
        key, secret = self._credentials.app()
        body = _token_call(
            {
                "client_key": key,
                "client_secret": secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
            self._transport,
        )
        stored = _stored(body, now)
        if not stored["refresh_token"]:
            stored["refresh_token"] = refresh
        self._credentials.update(stored)
        return str(stored["access_token"])


# --- the API ----------------------------------------------------------------------------------


def map_error(code: str, message: str, status: int, *, write: bool) -> PublishingError:
    text = f"TikTok said {code}: {message[:160]}" if message else f"TikTok said {code}"
    if code in _AUTH_CODES or status == 401:
        return AuthFailed(f"{text}; run `dk connect tiktok` again")
    if code in _RATE_CODES or status == 429:
        return RateLimited(RATE_WAIT)
    if code == _UNAUDITED:
        return Rejected(
            "TikTok only lets an app that has not passed its audit post to a private account. "
            "Set the TikTok account to private, or wait for the audit"
        )
    if code in _SERVER_CODES or status >= 500:
        return UnknownOutcome(text) if write else Retryable(text)
    return Rejected(text)


class HttpTikTokApi:
    def __init__(self, tokens: TikTokTokens, transport: httpx.BaseTransport | None = None) -> None:
        self._tokens = tokens
        self._transport = transport

    def _post(self, path: str, body: Mapping[str, Any], *, write: bool) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        try:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
                response = client.post(f"{API}{path}", json=dict(body), headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise Retryable(f"could not reach TikTok ({type(exc).__name__})") from None
        except httpx.HTTPError as exc:
            text = f"no answer from TikTok ({type(exc).__name__})"
            raise (UnknownOutcome(text) if write else Retryable(text)) from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") or {}
        code = str(error.get("code") or ("ok" if response.status_code == 200 else "http_error"))
        if response.status_code == 200 and code == "ok":
            return dict(payload.get("data") or {})
        raise map_error(code, str(error.get("message") or ""), response.status_code, write=write)

    def creator_info(self) -> CreatorInfo:
        data = self._post("/v2/post/publish/creator_info/query/", {}, write=False)
        return CreatorInfo(
            username=str(data.get("creator_username") or ""),
            nickname=str(data.get("creator_nickname") or ""),
            privacy_options=tuple(data.get("privacy_level_options") or ()),
            comment_disabled=bool(data.get("comment_disabled")),
            duet_disabled=bool(data.get("duet_disabled")),
            stitch_disabled=bool(data.get("stitch_disabled")),
            max_duration_s=int(data.get("max_video_post_duration_sec") or 0),
        )

    def init_video(
        self, *, post_info: Mapping[str, Any], size: int, chunk: int, chunks: int
    ) -> UploadSlot:
        data = self._post(
            "/v2/post/publish/video/init/",
            {
                "post_info": dict(post_info),
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk,
                    "total_chunk_count": chunks,
                },
            },
            write=True,
        )
        if not data.get("publish_id") or not data.get("upload_url"):
            raise UnknownOutcome("TikTok answered without a publish id")
        return UploadSlot(str(data["publish_id"]), str(data["upload_url"]))

    def upload(self, slot: UploadSlot, path: str, *, chunk: int) -> None:
        size = os.path.getsize(path)
        chunks = max(1, size // chunk)
        with (
            open(path, "rb") as handle,
            httpx.Client(timeout=UPLOAD_TIMEOUT, transport=self._transport) as client,
        ):
            for index in range(chunks):
                start = index * chunk
                last = index == chunks - 1
                length = size - start if last else chunk
                handle.seek(start)
                data = handle.read(length)
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{start + length - 1}/{size}",
                }
                try:
                    response = client.put(slot.upload_url, content=data, headers=headers)
                except httpx.HTTPError as exc:
                    text = f"the upload to TikTok broke ({type(exc).__name__})"
                    # Only the last chunk can make TikTok publish; losing its answer is uncertain.
                    raise (UnknownOutcome(text) if last else Retryable(text)) from None
                expected = 201 if last else 206
                if response.status_code not in (expected, 201, 206):
                    if response.status_code == 403:
                        raise Retryable("the TikTok upload address expired; starting over")
                    raise Rejected(f"TikTok refused the upload (HTTP {response.status_code})")

    def status(self, publish_id: str) -> PostStatus:
        data = self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id}, write=False)
        return PostStatus(
            status=str(data.get("status") or ""),
            fail_reason=str(data["fail_reason"]) if data.get("fail_reason") else None,
            post_ids=tuple(str(i) for i in data.get("publicaly_available_post_id") or ()),
        )
