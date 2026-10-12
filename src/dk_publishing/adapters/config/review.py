"""Is the owner's review page on? One answer, read by the page, the digest and the Sheet."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from dk_publishing.domain import review as links

_LOG = logging.getLogger("dk.review")


def review_settings(env: Mapping[str, str]) -> tuple[str, str] | None:
    """(secret, public address) of the owner's review page, or None when it is switched off: no
    secret, no address, or either one unusable (said in the log, without the value). The page, the
    digest's link and the Calendar's `watch` column all follow this one answer (ADR 0025)."""
    secret, base = env.get("REVIEW_SECRET", "").strip(), env.get("REVIEW_BASE_URL", "").strip()
    if not secret:
        return None
    if not links.secret_is_usable(secret):
        _LOG.warning(
            "review.secret is shorter than %d characters: the review page is off. "
            "Run `dk review init --rotate`.",
            links.MIN_SECRET_LENGTH,
        )
        return None
    local = base.startswith(("http://localhost", "http://127.0.0.1"))
    if not (base.startswith("https://") or local):
        _LOG.warning("review.public_base_url must start with https://: the review page is off.")
        return None
    return secret, links.clean_base(base)
