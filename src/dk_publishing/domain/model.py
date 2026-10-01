"""Core entities. Post, MediaAsset and Rendition arrive with the use cases that need them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.timezones import ensure_utc


class ActorKind(StrEnum):
    HUMAN = "human"
    SYSTEM = "system"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    name: str


@dataclass(frozen=True, slots=True)
class Variant:
    """One post on one account. Its status changes only through `lifecycle.transition`."""

    id: str
    tenant_id: str
    post_id: str
    platform: str
    account_id: str
    publish_at: datetime
    status: VariantStatus = VariantStatus.DRAFT
    version: int = 0  # bumped on every transition; the publish step compare-and-sets on it
    snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "publish_at", ensure_utc(self.publish_at))


@dataclass(frozen=True, slots=True)
class VariantEvent:
    """Append-only record of one transition. `seq` equals the variant's version after it."""

    variant_id: str
    seq: int
    from_status: VariantStatus
    to_status: VariantStatus
    actor: Actor
    reason: str
    at: datetime
