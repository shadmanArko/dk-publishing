from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from dk_publishing.application.ports import AccountInfo, DuplicateAccount, SyncVariant
from dk_publishing.domain.model import Variant
from dk_publishing.domain.sheet import RawRow
from dk_publishing.domain.snapshot import snapshot_hash
from dk_publishing.domain.status import VariantStatus

Conn = psycopg.Connection[tuple[Any, ...]]

_EDITABLE = (VariantStatus.DRAFT.value, VariantStatus.INVALID.value)


class PostgresSyncRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def accounts(self, tenant_id: str) -> list[AccountInfo]:
        rows = self._conn.execute(
            """SELECT id::text, platform, display_name, status FROM publishing.social_accounts
               WHERE tenant_id = %s ORDER BY platform, lower(display_name)""",
            (tenant_id,),
        ).fetchall()
        return [AccountInfo(r[0], r[1], r[2], r[3]) for r in rows]

    def add_account(
        self, tenant_id: str, platform: str, display_name: str, external_id: str
    ) -> str:
        try:
            with self._conn.transaction():
                row = self._conn.execute(
                    """INSERT INTO publishing.social_accounts
                           (tenant_id, platform, external_id, display_name)
                       VALUES (%s, %s, %s, %s) RETURNING id::text""",
                    (tenant_id, platform, external_id, display_name),
                ).fetchone()
        except errors.UniqueViolation as exc:
            raise DuplicateAccount(f"{platform}: {display_name}") from exc
        assert row is not None
        return str(row[0])

    def upsert_post(self, tenant_id: str, post_key: str, title: str) -> str:
        row = self._conn.execute(
            """INSERT INTO publishing.posts (tenant_id, post_key, title, source)
               VALUES (%s, %s, %s, 'human')
               ON CONFLICT (tenant_id, post_key)
               DO UPDATE SET title = EXCLUDED.title, updated_at = now()
               RETURNING id::text""",
            (tenant_id, post_key, title or None),
        ).fetchone()
        assert row is not None
        return str(row[0])

    def variants(self, tenant_id: str) -> list[SyncVariant]:
        rows = self._conn.execute(
            """SELECT v.id::text, v.tenant_id, v.post_id::text, v.platform, v.account_id::text,
                      v.publish_at, v.status, v.version, v.snapshot_hash,
                      p.post_key, p.title, a.display_name, v.source_hash, v.external_url,
                      (SELECT e.reason FROM publishing.variant_events e
                        WHERE e.variant_id = v.id ORDER BY e.seq DESC LIMIT 1)
               FROM publishing.variants v
               JOIN publishing.posts p ON p.tenant_id = v.tenant_id AND p.id = v.post_id
               JOIN publishing.social_accounts a
                    ON a.tenant_id = v.tenant_id AND a.id = v.account_id
               WHERE v.tenant_id = %s
               ORDER BY v.publish_at, v.id""",
            (tenant_id,),
        ).fetchall()
        return [
            SyncVariant(
                variant=Variant(
                    id=r[0],
                    tenant_id=r[1],
                    post_id=r[2],
                    platform=r[3],
                    account_id=r[4],
                    publish_at=r[5],
                    status=VariantStatus(r[6]),
                    version=r[7],
                    snapshot_hash=r[8],
                ),
                post_key=r[9],
                title=r[10],
                account_name=r[11],
                source_hash=r[12],
                external_url=r[13],
                last_reason=r[14],
            )
            for r in rows
        ]

    def update_inputs(self, variant_id: str, *, publish_at: datetime, source_hash: str) -> bool:
        cursor = self._conn.execute(
            """UPDATE publishing.variants
               SET publish_at = %s, source_hash = %s, updated_at = now()
               WHERE id = %s::uuid AND status = ANY(%s)""",
            (publish_at, source_hash, variant_id, list(_EDITABLE)),
        )
        return cursor.rowcount == 1

    def save_snapshots(self, tenant_id: str, sync_id: str, rows: Sequence[RawRow]) -> None:
        with self._conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO publishing.sheet_snapshots
                       (tenant_id, sync_id, tab, row_index, cells, row_hash)
                   VALUES (%s, %s::uuid, %s, %s, %s, %s)""",
                [
                    (
                        tenant_id,
                        sync_id,
                        r.tab,
                        r.row,
                        Jsonb(dict(r.cells)),
                        snapshot_hash(dict(r.cells)),
                    )
                    for r in rows
                ],
            )


def new_external_id(platform: str) -> str:
    """A placeholder id for an account that is not connected to a real platform (dry-run)."""
    return f"dry-run-{platform}-{uuid.uuid4().hex[:8]}"
