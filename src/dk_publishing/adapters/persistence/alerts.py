from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from dk_publishing.application.ports import AlertEvent, DigestItem

Conn = psycopg.Connection[tuple[Any, ...]]

# States a person must hear about the moment they happen.
ATTENTION = ("failed", "expired", "unknown")

_ITEM = """SELECT coalesce(p.title, ''), v.platform, coalesce(a.display_name, v.platform),
                  v.status, v.publish_at, v.published_at,
                  (SELECT e.reason FROM publishing.variant_events e
                    WHERE e.variant_id = v.id ORDER BY e.seq DESC LIMIT 1)
           FROM publishing.variants v
           JOIN publishing.posts p ON p.tenant_id = v.tenant_id AND p.id = v.post_id
           JOIN publishing.social_accounts a ON a.tenant_id = v.tenant_id AND a.id = v.account_id"""


class PostgresAlertRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def unsent_events(self, tenant_id: str, since: datetime, limit: int) -> list[AlertEvent]:
        rows = self._conn.execute(
            """SELECT 'event:' || e.variant_id::text || ':' || e.seq, e.variant_id::text,
                      coalesce(p.title, ''), v.platform, coalesce(a.display_name, v.platform),
                      e.to_status, e.reason, e.at, v.publish_at
               FROM publishing.variant_events e
               JOIN publishing.variants v ON v.tenant_id = e.tenant_id AND v.id = e.variant_id
               JOIN publishing.posts p ON p.tenant_id = v.tenant_id AND p.id = v.post_id
               JOIN publishing.social_accounts a
                 ON a.tenant_id = v.tenant_id AND a.id = v.account_id
               WHERE e.tenant_id = %s AND e.at >= %s AND e.to_status = ANY(%s)
                 AND NOT EXISTS (
                     SELECT 1 FROM publishing.alerts_sent s
                     WHERE s.tenant_id = e.tenant_id
                       AND s.key = 'event:' || e.variant_id::text || ':' || e.seq)
               ORDER BY e.at, e.seq LIMIT %s""",
            (tenant_id, since, list(ATTENTION), limit),
        ).fetchall()
        return [AlertEvent(*row) for row in rows]

    def mark_sent(self, tenant_id: str, key: str, now: datetime) -> bool:
        row = self._conn.execute(
            """INSERT INTO publishing.alerts_sent (tenant_id, key, sent_at) VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING RETURNING 1""",
            (tenant_id, key, now),
        ).fetchone()
        return row is not None

    def was_sent(self, tenant_id: str, key: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM publishing.alerts_sent WHERE tenant_id = %s AND key = %s",
                (tenant_id, key),
            ).fetchone()
            is not None
        )

    def results(self, tenant_id: str, start: datetime, end: datetime) -> list[DigestItem]:
        rows = self._conn.execute(
            f"""{_ITEM} WHERE v.tenant_id = %s AND v.publish_at >= %s AND v.publish_at < %s
                  AND v.status IN ('published', 'failed', 'expired', 'unknown')
                ORDER BY v.publish_at, v.platform""",
            (tenant_id, start, end),
        ).fetchall()
        return [DigestItem(*row) for row in rows]

    def upcoming(self, tenant_id: str, start: datetime, end: datetime) -> list[DigestItem]:
        rows = self._conn.execute(
            f"""{_ITEM} WHERE v.tenant_id = %s AND v.publish_at >= %s AND v.publish_at < %s
                  AND v.status IN ('approved', 'preparing', 'prepared', 'scheduling_native',
                                   'scheduled_native', 'publishing')
                ORDER BY v.publish_at, v.platform""",
            (tenant_id, start, end),
        ).fetchall()
        return [DigestItem(*row) for row in rows]
