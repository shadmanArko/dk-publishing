"""Webhook events and signatures: the rules a receiver can rely on (ADR 0023, guide 02).

A message is `POST <url>` with a JSON body and these headers:
  X-DK-Event, X-DK-Event-Id (stable across retries: dedupe on it), X-DK-Delivery,
  X-DK-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>" with the endpoint secret>
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from dk_publishing.domain.status import VariantStatus

S = VariantStatus

# A variant moving INTO one of these states is reported. States in between (preparing,
# publishing...) are not: the client needs outcomes, not internals.
EVENT_FOR_STATUS: Mapping[VariantStatus, str] = MappingProxyType(
    {
        S.APPROVED: "post.approved",
        S.PUBLISHED: "post.published",
        S.FAILED: "post.failed",
        S.EXPIRED: "post.expired",
        S.UNKNOWN: "post.needs_check",
        S.CANCELLED: "post.cancelled",
        S.INVALID: "post.invalid",
    }
)
EVENTS = tuple(EVENT_FOR_STATUS.values())
PING = "ping"
TOLERANCE_SECONDS = 300  # a receiver should refuse a signature older than this


def body_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes that are signed and sent: compact, keys in a stable order."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify(
    secret: str, header: str, body: bytes, now: int, tolerance: int = TOLERANCE_SECONDS
) -> bool:
    """What a receiver does: recompute, compare in constant time, refuse stale timestamps."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(now - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), header)
