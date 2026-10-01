"""The single place a variant's state may change."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from dk_publishing.domain.model import Actor, Variant, VariantEvent
from dk_publishing.domain.status import ALLOWED, REQUIRES_SNAPSHOT, VariantStatus
from dk_publishing.domain.timezones import ensure_utc


class IllegalTransition(Exception):
    """The move breaks the lifecycle rules. A bug in the caller, not a platform error."""


def transition(
    variant: Variant,
    to: VariantStatus,
    *,
    actor: Actor,
    reason: str,
    at: datetime,
    snapshot_hash: str | None = None,
) -> tuple[Variant, VariantEvent]:
    """Move `variant` to `to`, returning the new variant and the event to append.

    `snapshot_hash` is accepted only on the move that freezes a snapshot (into APPROVED, from a
    state that has none yet). Going back to DRAFT clears it, so an edit forces a fresh approval.
    """
    if not reason.strip():
        raise IllegalTransition("every transition needs a reason")
    if to not in ALLOWED[variant.status]:
        raise IllegalTransition(f"{variant.status} -> {to} is not allowed")

    if to is VariantStatus.DRAFT:
        new_hash = None
        _reject_hash(snapshot_hash, to)
    elif to is VariantStatus.APPROVED and variant.snapshot_hash is None:
        if snapshot_hash is None:
            raise IllegalTransition("approving needs the snapshot hash")
        new_hash = snapshot_hash
    else:
        _reject_hash(snapshot_hash, to)
        new_hash = variant.snapshot_hash

    if to in REQUIRES_SNAPSHOT and new_hash is None:
        raise IllegalTransition(f"{to} requires an approved snapshot")

    moved = replace(variant, status=to, version=variant.version + 1, snapshot_hash=new_hash)
    event = VariantEvent(
        variant_id=variant.id,
        seq=moved.version,
        from_status=variant.status,
        to_status=to,
        actor=actor,
        reason=reason,
        at=ensure_utc(at),
    )
    return moved, event


def _reject_hash(snapshot_hash: str | None, to: VariantStatus) -> None:
    if snapshot_hash is not None:
        raise IllegalTransition(f"a snapshot hash cannot be supplied when moving to {to}")
