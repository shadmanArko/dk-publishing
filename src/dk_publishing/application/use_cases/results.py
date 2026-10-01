from __future__ import annotations

from enum import StrEnum


class RunResult(StrEnum):
    """What a use case did. A lost race or a stale run key is normal, not an error."""

    APPROVED = "approved"
    INVALID = "invalid"
    PREPARED = "prepared"
    SCHEDULED = "scheduled"  # handed to the platform, which will publish it at the slot
    PUBLISHED = "published"
    RETRY_SCHEDULED = "retry_scheduled"
    PARKED = "parked"  # needs a person (re-authorise the account); nothing is scheduled
    FLAGGED_UNKNOWN = "flagged_unknown"
    NOT_LIVE = "not_live"  # reconciliation confirmed the post never went out
    FAILED = "failed"
    EXPIRED = "expired"
    LOST_RACE = "lost_race"  # another run owns this variant now
    SKIPPED = "skipped"  # the run key no longer matches the variant
