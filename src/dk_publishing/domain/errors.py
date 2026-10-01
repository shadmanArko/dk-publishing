"""The five outcomes a platform call can map into. Adapters translate; nothing else is raised."""

from __future__ import annotations

from datetime import timedelta


class PublishingError(Exception):
    """Base for every error the domain recognises."""


class Retryable(PublishingError):
    """Failed before the platform acted (timeout, 5xx before any response). Safe to retry."""


class RateLimited(PublishingError):
    """The platform asked us to wait."""

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__(f"rate limited, retry after {retry_after}")
        self.retry_after = retry_after


class AuthFailed(PublishingError):
    """Token expired or revoked. The account needs re-authorisation."""


class Rejected(PublishingError):
    """The platform refused the content. Retrying the same content will not help."""


class UnknownOutcome(PublishingError):
    """The request may or may not have taken effect. Never retry blindly: reconcile first."""
