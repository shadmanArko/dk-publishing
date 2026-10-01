from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from dk_publishing.application.ports import DueAction, Handle
from dk_publishing.domain.model import Actor, ActorKind, Variant, VariantEvent
from dk_publishing.domain.planning import Action, NextStep
from dk_publishing.domain.publishing import LivePost
from dk_publishing.domain.snapshot import snapshot_hash
from dk_publishing.domain.status import VariantStatus

Conn = psycopg.Connection[tuple[Any, ...]]

_VARIANT_COLUMNS = (
    "id::text, tenant_id, post_id::text, platform, account_id::text, "
    "publish_at, status, version, snapshot_hash"
)


def _variant(row: tuple[Any, ...]) -> Variant:
    return Variant(
        id=row[0],
        tenant_id=row[1],
        post_id=row[2],
        platform=row[3],
        account_id=row[4],
        publish_at=row[5],
        status=VariantStatus(row[6]),
        version=row[7],
        snapshot_hash=row[8],
    )


class PostgresVariantRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def add(self, variant: Variant) -> None:
        if variant.status is not VariantStatus.DRAFT or variant.version != 0:
            raise ValueError("a new variant must be a fresh draft (version 0)")
        if variant.snapshot_hash is not None:
            raise ValueError("a new variant cannot carry a snapshot")
        self._conn.execute(
            """INSERT INTO publishing.variants
                   (id, tenant_id, post_id, platform, account_id, publish_at, status, version)
               VALUES (%s::uuid, %s, %s::uuid, %s, %s::uuid, %s, %s, %s)""",
            (
                variant.id,
                variant.tenant_id,
                variant.post_id,
                variant.platform,
                variant.account_id,
                variant.publish_at,
                variant.status.value,
                variant.version,
            ),
        )

    def get(self, variant_id: str) -> Variant | None:
        row = self._conn.execute(
            f"SELECT {_VARIANT_COLUMNS} FROM publishing.variants WHERE id = %s::uuid",
            (variant_id,),
        ).fetchone()
        return None if row is None else _variant(row)

    def snapshot_of(self, variant_id: str) -> Mapping[str, Any] | None:
        row = self._conn.execute(
            "SELECT snapshot FROM publishing.variants WHERE id = %s::uuid", (variant_id,)
        ).fetchone()
        return None if row is None else row[0]

    def handle_of(self, variant_id: str) -> Handle | None:
        row = self._conn.execute(
            "SELECT native_handle FROM publishing.variants WHERE id = %s::uuid", (variant_id,)
        ).fetchone()
        return None if row is None else row[0]

    def stale(self, status: VariantStatus, updated_before: datetime, limit: int) -> list[Variant]:
        rows = self._conn.execute(
            f"""SELECT {_VARIANT_COLUMNS} FROM publishing.variants
               WHERE status = %s AND updated_at < %s ORDER BY updated_at, id LIMIT %s""",
            (status.value, updated_before, limit),
        ).fetchall()
        return [_variant(r) for r in rows]

    def events(self, variant_id: str) -> list[VariantEvent]:
        rows = self._conn.execute(
            """SELECT variant_id::text, seq, from_status, to_status, actor_kind, actor_name,
                      reason, at
               FROM publishing.variant_events WHERE variant_id = %s::uuid ORDER BY seq""",
            (variant_id,),
        ).fetchall()
        return [
            VariantEvent(
                variant_id=r[0],
                seq=r[1],
                from_status=VariantStatus(r[2]),
                to_status=VariantStatus(r[3]),
                actor=Actor(ActorKind(r[4]), r[5]),
                reason=r[6],
                at=r[7],
            )
            for r in rows
        ]

    def apply(
        self,
        old: Variant,
        new: Variant,
        event: VariantEvent,
        next_step: NextStep | None,
        *,
        snapshot: Mapping[str, Any] | None = None,
        handle: Handle | None = None,
        live: LivePost | None = None,
    ) -> bool:
        _check_consistent(old, new, event, snapshot)

        sets = [
            "status = %s",
            "version = %s",
            "publish_at = %s",
            "snapshot_hash = %s",
            "next_action = %s",
            "next_action_at = %s",
            "updated_at = %s",
        ]
        params: list[Any] = [
            new.status.value,
            new.version,
            new.publish_at,
            new.snapshot_hash,
            None if next_step is None else next_step.action.value,
            None if next_step is None else next_step.at,
            event.at,  # the domain clock, so staleness is testable and consistent
        ]
        if new.snapshot_hash is None:
            sets.append("snapshot = NULL")
        elif snapshot is not None:
            sets.append("snapshot = %s")
            params.append(Jsonb(snapshot))
        if new.status is VariantStatus.DRAFT:
            sets.append("native_handle = NULL")  # an edit invalidates anything prepared
        elif handle is not None:
            sets.append("native_handle = %s")
            params.append(Jsonb(handle))
        if new.status is VariantStatus.PUBLISHED:
            sets.append("published_at = %s")
            params.append(event.at)
            if live is not None:
                sets.extend(["external_id = %s", "external_url = %s"])
                params.extend([live.external_id, live.url])

        # The compare-and-set. Under READ COMMITTED a racing writer blocks on the row lock, then
        # re-checks this WHERE against the winner's committed row and matches nothing.
        cursor = self._conn.execute(
            f"UPDATE publishing.variants SET {', '.join(sets)} "
            "WHERE id = %s::uuid AND tenant_id = %s AND version = %s AND status = %s",
            (*params, old.id, old.tenant_id, old.version, old.status.value),
        )
        if cursor.rowcount == 0:
            return False

        self._conn.execute(
            """INSERT INTO publishing.variant_events
                   (tenant_id, variant_id, seq, from_status, to_status,
                    actor_kind, actor_name, reason, at)
               VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)""",
            (
                new.tenant_id,
                event.variant_id,
                event.seq,
                event.from_status.value,
                event.to_status.value,
                event.actor.kind.value,
                event.actor.name,
                event.reason,
                event.at,
            ),
        )
        return True

    def due(self, now: datetime, limit: int) -> list[DueAction]:
        rows = self._conn.execute(
            """SELECT id::text, next_action, next_action_at, version, platform, account_id::text
               FROM publishing.variants
               WHERE next_action_at <= %s
               ORDER BY next_action_at, id
               LIMIT %s""",
            (now, limit),
        ).fetchall()
        return [DueAction(r[0], Action(r[1]), r[2], r[3], r[4], r[5]) for r in rows]


def _check_consistent(
    old: Variant, new: Variant, event: VariantEvent, snapshot: Mapping[str, Any] | None
) -> None:
    """Reject a mismatched (old, new, event) triple before touching the database."""
    if not (old.id == new.id == event.variant_id and old.tenant_id == new.tenant_id):
        raise ValueError("old, new and event must describe the same variant")
    if new.version != old.version + 1 or event.seq != new.version:
        raise ValueError("version must advance by exactly one and match the event sequence")
    if event.from_status is not old.status or event.to_status is not new.status:
        raise ValueError("event does not describe the move from old to new")
    freezing = new.snapshot_hash is not None and old.snapshot_hash is None
    if freezing:
        if snapshot is None:
            raise ValueError("freezing a snapshot requires its content")
        if snapshot_hash(snapshot) != new.snapshot_hash:
            raise ValueError("snapshot content does not match its hash")
    elif snapshot is not None:
        raise ValueError("snapshot content is only accepted on the transition that freezes it")
