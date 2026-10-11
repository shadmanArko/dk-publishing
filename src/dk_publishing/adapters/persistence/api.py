from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from dk_publishing.application.ports import (
    ApiKey,
    ApiKeyInfo,
    DuplicateKey,
    DuplicatePost,
    StoredPost,
    UploadedMedia,
)

Conn = psycopg.Connection[tuple[Any, ...]]


class PostgresApiRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def add_key(self, tenant_id: str, name: str, key_hash: str, scopes: Sequence[str]) -> str:
        try:
            with self._conn.transaction():
                row = self._conn.execute(
                    """INSERT INTO publishing.api_keys (tenant_id, name, key_hash, scopes)
                       VALUES (%s, %s, %s, %s) RETURNING id::text""",
                    (tenant_id, name, key_hash, list(scopes)),
                ).fetchone()
        except errors.UniqueViolation as exc:
            raise DuplicateKey(name) from exc
        assert row is not None
        return str(row[0])

    def key_by_hash(self, key_hash: str) -> ApiKey | None:
        row = self._conn.execute(
            """SELECT id::text, tenant_id, name, scopes FROM publishing.api_keys
               WHERE key_hash = %s AND revoked_at IS NULL""",
            (key_hash,),
        ).fetchone()
        return ApiKey(row[0], row[1], row[2], tuple(row[3])) if row else None

    def keys(self, tenant_id: str) -> list[ApiKeyInfo]:
        rows = self._conn.execute(
            """SELECT name, scopes, created_at, last_used_at, revoked_at IS NOT NULL
               FROM publishing.api_keys WHERE tenant_id = %s ORDER BY created_at""",
            (tenant_id,),
        ).fetchall()
        return [ApiKeyInfo(r[0], tuple(r[1]), r[2], r[3], r[4]) for r in rows]

    def revoke_key(self, tenant_id: str, name: str, at: datetime) -> bool:
        cursor = self._conn.execute(
            """UPDATE publishing.api_keys SET revoked_at = %s
               WHERE tenant_id = %s AND name = %s AND revoked_at IS NULL""",
            (at, tenant_id, name),
        )
        return cursor.rowcount == 1

    def touch_key(self, key_id: str, at: datetime) -> None:
        self._conn.execute(
            "UPDATE publishing.api_keys SET last_used_at = %s WHERE id = %s::uuid", (at, key_id)
        )

    def add_media(
        self, tenant_id: str, filename: str, sha256: str, size: int, created_by: str
    ) -> str:
        row = self._conn.execute(
            """INSERT INTO publishing.api_media (tenant_id, filename, sha256, bytes, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING id::text""",
            (tenant_id, filename, sha256, size, created_by),
        ).fetchone()
        assert row is not None
        return str(row[0])

    def media(self, tenant_id: str, ids: Sequence[str]) -> dict[str, UploadedMedia]:
        valid = [i for i in ids if _is_uuid(i)]
        if not valid:
            return {}
        rows = self._conn.execute(
            """SELECT id::text, filename, sha256, bytes, purged_at FROM publishing.api_media
               WHERE tenant_id = %s AND id = ANY(%s::uuid[])""",
            (tenant_id, valid),
        ).fetchall()
        return {r[0]: UploadedMedia(r[0], r[1], r[2], r[3], r[4] is not None) for r in rows}

    def media_bytes(self, tenant_id: str, created_by: str) -> int:
        row = self._conn.execute(
            """SELECT coalesce(sum(bytes), 0) FROM publishing.api_media
               WHERE tenant_id = %s AND created_by = %s AND purged_at IS NULL""",
            (tenant_id, created_by),
        ).fetchone()
        return int(row[0]) if row else 0

    def media_since(self, tenant_id: str, since: datetime) -> list[UploadedMedia]:
        rows = self._conn.execute(
            """SELECT id::text, filename, sha256, bytes, purged_at FROM publishing.api_media
               WHERE tenant_id = %s AND created_at >= %s""",
            (tenant_id, since),
        ).fetchall()
        return [UploadedMedia(r[0], r[1], r[2], r[3], r[4] is not None) for r in rows]

    def mark_media_purged(self, tenant_id: str, sha256s: Sequence[str], at: datetime) -> int:
        if not sha256s:
            return 0
        cursor = self._conn.execute(
            """UPDATE publishing.api_media SET purged_at = %s
               WHERE tenant_id = %s AND sha256 = ANY(%s) AND purged_at IS NULL""",
            (at, tenant_id, list(sha256s)),
        )
        return cursor.rowcount

    def post(self, tenant_id: str, post_key: str) -> StoredPost | None:
        row = self._conn.execute(
            f"{_POST} WHERE tenant_id = %s AND post_key = %s", (tenant_id, post_key)
        ).fetchone()
        return _stored(row) if row else None

    def create_post(
        self, tenant_id: str, post_key: str, label: str | None, request_hash: str, created_by: str
    ) -> str:
        try:
            with self._conn.transaction():
                row = self._conn.execute(
                    """INSERT INTO publishing.posts
                           (tenant_id, post_key, title, source, request_hash, created_by)
                       VALUES (%s, %s, %s, 'agent', %s, %s) RETURNING id::text""",
                    (tenant_id, post_key, label, request_hash, created_by),
                ).fetchone()
        except errors.UniqueViolation as exc:
            raise DuplicatePost(post_key) from exc
        assert row is not None
        return str(row[0])

    def posts(self, tenant_id: str, limit: int) -> list[StoredPost]:
        rows = self._conn.execute(
            f"{_POST} WHERE tenant_id = %s AND source = 'agent' ORDER BY created_at DESC LIMIT %s",
            (tenant_id, limit),
        ).fetchall()
        return [_stored(r) for r in rows]

    def save_content(self, tenant_id: str, variant_id: str, content: Mapping[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO publishing.api_content (variant_id, tenant_id, content)
               VALUES (%s::uuid, %s, %s)
               ON CONFLICT (variant_id) DO UPDATE SET content = EXCLUDED.content""",
            (variant_id, tenant_id, Jsonb(dict(content))),
        )

    def content(self, variant_id: str) -> Mapping[str, Any] | None:
        row = self._conn.execute(
            "SELECT content FROM publishing.api_content WHERE variant_id = %s::uuid", (variant_id,)
        ).fetchone()
        return dict(row[0]) if row else None


_POST = "SELECT id::text, post_key, title, source, request_hash, created_by FROM publishing.posts"


def _stored(row: tuple[Any, ...]) -> StoredPost:
    return StoredPost(row[0], row[1], row[2], row[3], row[4], row[5])


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
