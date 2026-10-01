"""One call to a platform, recorded before it is made and completed after."""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    PREPARE = "prepare"
    SCHEDULE_NATIVE = "schedule_native"
    PUBLISH = "publish"
    RECONCILE = "reconcile"


class Outcome(StrEnum):
    """Mirrors the five domain errors, plus success."""

    OK = "ok"
    RETRYABLE = "retryable"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
