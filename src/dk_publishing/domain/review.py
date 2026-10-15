"""Signed review links and sessions for the owner's daily review page (ADR 0025).

Pure and stdlib only. A *link* is what the Telegram digest carries: it proves "the owner asked for
this" for 24 hours and is exchanged once for a *session* cookie. Both are `payload.signature`:
the payload says what the token is for (`purpose`) and when it ends, the signature is an
HMAC-SHA256 under a key derived from the review secret. A token made for one purpose is refused for
the other, and the signature is checked (in constant time) before anything inside is believed.

Nothing here logs, and no error message ever contains a token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from urllib.parse import quote, urlencode

LINK = "review-link"  # what the digest carries
SESSION = "review-session"  # what the cookie carries
LINK_TTL = timedelta(hours=24)
SESSION_TTL = timedelta(hours=12)
SIGNED_IN_TTL = timedelta(
    days=365
)  # email + password sign-in (ADR 0026): a year, renewed on every visit
MIN_SECRET_LENGTH = 32  # `dk review init` makes 43; a hand-typed short one is refused
MAX_TOKEN_LENGTH = 512

_KEY_LABEL = b"dk-publishing/review/v1"


class Verdict(StrEnum):
    OK = "ok"
    EXPIRED = "expired"  # genuine and signed, but past its end
    INVALID = "invalid"  # malformed, tampered, or made for another purpose


@dataclass(frozen=True, slots=True)
class Claims:
    purpose: str
    expires: datetime
    ident: str  # random per token; the session's id, which its CSRF token is derived from
    who: str = ""  # the signed-in person's email (ADR 0026); empty for a link or a link's session


def secret_is_usable(secret: str) -> bool:
    return len(secret.strip()) >= MIN_SECRET_LENGTH


def new_secret() -> str:
    """A fresh secret for `dk review init`: 256 random bits, URL-safe."""
    return secrets.token_urlsafe(32)


def _key(secret: str) -> bytes:
    return hmac.new(secret.strip().encode(), _KEY_LABEL, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, payload: str) -> bytes:
    return hmac.new(_key(secret), payload.encode("ascii"), hashlib.sha256).digest()


def issue(
    secret: str, purpose: str, now: datetime, ttl: timedelta, who: str = "", ident: str = ""
) -> str:
    """A signed token for `purpose`, valid from `now` for `ttl`; `who` names who signed in."""
    claims = {
        "p": purpose,
        "e": int((now + ttl).astimezone(UTC).timestamp()),
        "i": ident or secrets.token_hex(8),  # a renewal keeps its id, so open forms stay valid
    }
    if who:
        claims["u"] = who
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_b64(_mac(secret, payload))}"


def check(secret: str, token: str, purpose: str, now: datetime) -> tuple[Verdict, Claims | None]:
    """Is `token` a genuine, unexpired token for `purpose`? Never raises, never reveals why a
    forgery failed: the only distinction made is expired (after the signature held) versus not."""
    if not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 1:
        return Verdict.INVALID, None
    payload, _, signature = token.partition(".")
    try:
        supplied = _unb64(signature)
        # Constant time, and before the payload is parsed: an unsigned payload is never read.
        if not hmac.compare_digest(supplied, _mac(secret, payload)):
            return Verdict.INVALID, None
        body = json.loads(_unb64(payload))
        kind, ends, ident = body["p"], body["e"], body["i"]
        who = body.get("u", "")
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        return Verdict.INVALID, None
    if not (
        isinstance(kind, str)
        and isinstance(ends, int)
        and isinstance(ident, str)
        and isinstance(who, str)
    ):
        return Verdict.INVALID, None
    if not hmac.compare_digest(kind.encode(), purpose.encode()):
        return Verdict.INVALID, None
    claims = Claims(kind, datetime.fromtimestamp(ends, UTC), ident, who)
    if claims.expires <= now:
        return Verdict.EXPIRED, None
    return Verdict.OK, claims


def csrf_token(secret: str, session: Claims) -> str:
    """The form token for this session: only a page served to the session's holder carries it."""
    return _b64(hmac.new(_key(secret), b"csrf:" + session.ident.encode(), hashlib.sha256).digest())


def csrf_ok(secret: str, session: Claims, supplied: str) -> bool:
    return hmac.compare_digest(supplied.encode(), csrf_token(secret, session).encode())


# --- addresses --------------------------------------------------------------------------------


def clean_base(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def link_url(base_url: str, token: str, day: date | None = None) -> str:
    """The address the digest carries: opening it signs the owner in, then goes to `day`."""
    query = {"t": token, **({"day": day.isoformat()} if day else {})}
    return f"{clean_base(base_url)}/review/enter?{urlencode(query)}"


def watch_url(base_url: str, day: date, post_key: str) -> str:
    """The Calendar tab's `watch` link: the review page for that day, scrolled to that idea. It
    carries no credential, so it is safe to keep in a Sheet; a signed-out visit explains itself."""
    return f"{clean_base(base_url)}/review?day={day.isoformat()}#i-{quote(post_key, safe='')}"


# --- failed attempts --------------------------------------------------------------------------


class FailureLimiter:
    """Refuses further attempts from a source after `limit` failures within `window`.

    Failures are forged or tampered links, cookies and form tokens; a missing or merely expired
    one is not an attack and is not counted. In memory, per process: it bounds guessing and noise,
    it is not what the security rests on (the signature is)."""

    def __init__(self, limit: int = 10, window: timedelta = timedelta(minutes=10)) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def _recent(self, source: str, now: datetime) -> deque[datetime]:
        hits = self._hits.setdefault(source, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if not hits:
            self._hits.pop(source, None)
        return hits

    def blocked(self, source: str, now: datetime) -> bool:
        with self._lock:
            return len(self._recent(source, now)) >= self._limit

    def record(self, source: str, now: datetime) -> None:
        with self._lock:
            if len(self._hits) > 1000:  # a flood of sources must not grow memory without end
                self._hits.clear()
            self._hits.setdefault(source, deque()).append(now)
