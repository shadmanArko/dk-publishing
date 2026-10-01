"""Variant states and the only moves between them (docs/architecture.md, "Variant lifecycle")."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class VariantStatus(StrEnum):
    DRAFT = "draft"
    INVALID = "invalid"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    PREPARING = "preparing"
    PREPARED = "prepared"
    SCHEDULING_NATIVE = "scheduling_native"
    SCHEDULED_NATIVE = "scheduled_native"
    PUBLISHING = "publishing"
    UNKNOWN = "unknown"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


S = VariantStatus

ALLOWED: MappingProxyType[VariantStatus, frozenset[VariantStatus]] = MappingProxyType(
    {
        S.DRAFT: frozenset({S.INVALID, S.PENDING_APPROVAL, S.APPROVED, S.CANCELLED}),
        S.INVALID: frozenset({S.DRAFT}),
        S.PENDING_APPROVAL: frozenset({S.APPROVED, S.DRAFT, S.CANCELLED}),
        # An edit while approved, prepared or natively scheduled invalidates the snapshot and sends
        # the variant back to DRAFT. In-flight states have no such exit: a run in progress finishes
        # first, so no half-created remote handle is orphaned; the edit applies where it settles.
        S.APPROVED: frozenset({S.PREPARING, S.SCHEDULING_NATIVE, S.CANCELLED, S.EXPIRED, S.DRAFT}),
        S.PREPARING: frozenset({S.PREPARED, S.APPROVED, S.FAILED}),
        S.PREPARED: frozenset({S.PUBLISHING, S.APPROVED, S.CANCELLED, S.EXPIRED, S.DRAFT}),
        S.SCHEDULING_NATIVE: frozenset({S.SCHEDULED_NATIVE, S.APPROVED, S.FAILED}),
        S.SCHEDULED_NATIVE: frozenset({S.PUBLISHED, S.UNKNOWN, S.CANCELLED, S.DRAFT}),
        S.PUBLISHING: frozenset({S.PUBLISHED, S.UNKNOWN, S.PREPARED, S.FAILED}),
        S.UNKNOWN: frozenset({S.PUBLISHED, S.PREPARED, S.FAILED}),
        # Live is final. Editing a failed, cancelled or expired row starts a fresh draft.
        S.PUBLISHED: frozenset(),
        S.FAILED: frozenset({S.DRAFT}),
        S.CANCELLED: frozenset({S.DRAFT}),
        S.EXPIRED: frozenset({S.DRAFT}),
    }
)

# Nothing more will happen to the post on the platform. Media retention counts from here.
FINAL = frozenset({S.PUBLISHED, S.FAILED, S.CANCELLED, S.EXPIRED})

IN_FLIGHT = frozenset({S.PREPARING, S.SCHEDULING_NATIVE, S.PUBLISHING})

# States that only exist for an approved snapshot, so the variant must carry its hash.
REQUIRES_SNAPSHOT = frozenset(
    {
        S.APPROVED,
        S.PREPARING,
        S.PREPARED,
        S.SCHEDULING_NATIVE,
        S.SCHEDULED_NATIVE,
        S.PUBLISHING,
        S.UNKNOWN,
        S.PUBLISHED,
    }
)
