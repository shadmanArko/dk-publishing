"""Shared plumbing for the Meta Graph API (Facebook, and later Instagram and Threads).

One client, one error mapping. Every failure becomes one of the five domain errors, and the
rule that keeps posts from being duplicated lives here: a call that changes something
(`idempotent=False`) is never reported as a plain, retryable failure once it may have reached
Meta. It is `UnknownOutcome`, which the use cases reconcile before anything is retried.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, BinaryIO

import httpx

from dk_publishing.adapters.platforms.tokens import TokenProvider
from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)

GRAPH = "https://graph.facebook.com"
GRAPH_VIDEO = "https://graph-video.facebook.com"
DEFAULT_RETRY_AFTER = timedelta(minutes=10)

# Graph error codes (https://developers.facebook.com/docs/graph-api/guides/error-handling)
RATE_LIMIT_CODES = {4, 17, 32, 613}
AUTH_CODES = {102, 190}  # session invalid / token expired or revoked
PERMISSION_CODES = {10, *range(200, 300)}
TRANSIENT_CODES = {1, 2}  # "unknown error" / "service temporarily unavailable"

_SECRET = re.compile(r"(access_token|client_secret)=[^&\s\"']+", re.IGNORECASE)
_BEARER = re.compile(r"(Bearer|OAuth)\s+[A-Za-z0-9._\-]+")


def redact(text: str) -> str:
    """Remove anything shaped like a token before text reaches a log or the Sheet."""
    return _BEARER.sub(r"\1 [redacted]", _SECRET.sub(r"\1=[redacted]", text))


def map_graph_error(
    status: int, body: Any, headers: httpx.Headers, *, idempotent: bool
) -> PublishingError:
    error = body.get("error", {}) if isinstance(body, dict) else {}
    code, message = error.get("code"), redact(str(error.get("message") or f"HTTP {status}"))
    detail = f"{message} (Meta code {code})" if code is not None else message

    if status == 429 or code in RATE_LIMIT_CODES:
        return RateLimited(_retry_after(headers))
    if code in AUTH_CODES or status == 401:
        return AuthFailed(detail)
    if code in PERMISSION_CODES:
        return AuthFailed(f"missing permission: {detail}")
    if status >= 500 or code in TRANSIENT_CODES:
        # Meta may or may not have acted. Only a read can be safely repeated.
        return Retryable(detail) if idempotent else UnknownOutcome(detail)
    return Rejected(detail)


def _retry_after(headers: httpx.Headers) -> timedelta:
    try:
        return timedelta(seconds=int(headers.get("retry-after", "")))
    except ValueError:
        return DEFAULT_RETRY_AFTER


class GraphClient:
    def __init__(
        self,
        *,
        version: str,
        tokens: TokenProvider,
        transport: httpx.BaseTransport | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        upload_timeout: float = 900.0,
    ) -> None:
        self._version = version
        self._tokens = tokens
        self._connect = connect_timeout
        self._read = read_timeout
        self._upload = upload_timeout
        self._client = httpx.Client(transport=transport)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """A read. Safe to repeat, so server trouble is Retryable."""
        query = {**(params or {}), "access_token": self._tokens.token()}
        return self._send("GET", f"{GRAPH}/{self._version}/{path}", idempotent=True, params=query)

    def post(
        self,
        path: str,
        data: dict[str, Any],
        *,
        file: tuple[str, BinaryIO] | None = None,
        video: bool = False,
    ) -> dict[str, Any]:
        """A write. Never retried here; trouble after the request left is UnknownOutcome."""
        body = {**data, "access_token": self._tokens.token()}
        host = GRAPH_VIDEO if video else GRAPH
        files = {file[0]: file[1]} if file else None
        return self._send(
            "POST",
            f"{host}/{self._version}/{path}",
            idempotent=False,
            data=body,
            files=files,
            timeout=self._upload if file else self._read,
        )

    def _send(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        limits = httpx.Timeout(timeout or self._read, connect=self._connect)
        try:
            response = self._client.request(method, url, timeout=limits, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never left this machine, so nothing can have happened at Meta.
            raise Retryable(f"could not reach Meta: {redact(str(exc))}") from None
        except httpx.HTTPError as exc:
            # Sent, but no usable answer (timeout, dropped connection): Meta may have acted.
            text = f"no answer from Meta: {type(exc).__name__}"
            raise (Retryable(text) if idempotent else UnknownOutcome(text)) from None

        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success:
            if isinstance(body, dict):
                return body
            text = "Meta answered with something that was not JSON"
            raise Retryable(text) if idempotent else UnknownOutcome(text)
        raise map_graph_error(response.status_code, body, response.headers, idempotent=idempotent)
