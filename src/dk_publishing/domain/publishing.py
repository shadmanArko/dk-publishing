"""Values that cross the boundary between the use cases and a platform adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dk_publishing.domain.model import Variant


@dataclass(frozen=True, slots=True)
class Violation:
    """A reason a variant cannot be published, worded for the person who edits the Sheet."""

    field: str
    message: str


@dataclass(frozen=True, slots=True)
class LivePost:
    external_id: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Rendition:
    """A media file shaped for one platform profile."""

    profile: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class VariantSnapshot:
    """Everything an adapter may use. `content` is the approved, hashed part."""

    variant_id: str
    tenant_id: str
    platform: str
    account_id: str
    publish_at: datetime
    content: Mapping[str, Any]


def snapshot_for(variant: Variant, content: Mapping[str, Any]) -> VariantSnapshot:
    return VariantSnapshot(
        variant_id=variant.id,
        tenant_id=variant.tenant_id,
        platform=variant.platform,
        account_id=variant.account_id,
        publish_at=variant.publish_at,
        content=content,
    )
