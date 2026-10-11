from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from dk_publishing.application.ports import (
    WebhookCandidate,
    WebhookDelivery,
    WebhookEndpoint,
)
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.webhooks import EVENT_FOR_STATUS

Conn = psycopg.Connection[tuple[Any, ...]]

_ENDPOINT = """SELECT w.id::text, w.api_key_id::text, k.name, w.url, w.events, w.active,
                      w.disabled_reason, w.created_at
               FROM publishing.webhook_endpoints w
               JOIN publishing.api_keys k ON k.id = w.api_key_id"""

_DELIVERY = """SELECT d.id::text, d.endpoint_id::text, w.url, w.secret, d.event_id, d.event_type,
                      d.payload, d.attempts, d.status, d.last_status, d.last_error,
                      d.next_attempt_at, d.created_at, d.delivered_at
               FROM publishing.webhook_deliveries d
               JOIN publishing.webhook_endpoints w ON w.id = d.endpoint_id"""


def _endpoint(r: tuple[Any, ...]) -> WebhookEndpoint:
    return WebhookEndpoint(r[0], r[1], r[2], r[3], tuple(r[4]), r[5], r[6], r[7])


def _delivery(r: tuple[Any, ...]) -> WebhookDelivery:
    return WebhookDelivery(
        id=r[0], endpoint_id=r[1], url=r[2], secret=r[3], event_id=r[4], event_type=r[5],
        payload=r[6], attempts=r[7], status=r[8], last_status=r[9], last_error=r[10],
        next_attempt_at=r[11], created_at=r[12], delivered_at=r[13],
    )  # fmt: skip


class PostgresWebhookRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def add_endpoint(
        self, tenant_id: str, key_id: str, url: str, secret: str, events: Sequence[str]
    ) -> WebhookEndpoint:
        row = self._conn.execute(
            """INSERT INTO publishing.webhook_endpoints (tenant_id, api_key_id, url, secret, events)
               VALUES (%s, %s::uuid, %s, %s, %s) RETURNING id::text""",
            (tenant_id, key_id, url, secret, list(events)),
        ).fetchone()
        assert row is not None
        [found] = [e for e in self.endpoints(tenant_id, key_id) if e.id == row[0]]
        return found

    def endpoints(self, tenant_id: str, key_id: str | None = None) -> list[WebhookEndpoint]:
        rows = self._conn.execute(
            f"""{_ENDPOINT} WHERE w.tenant_id = %s AND (%s::uuid IS NULL OR w.api_key_id = %s::uuid)
                ORDER BY w.created_at DESC""",
            (tenant_id, key_id, key_id),
        ).fetchall()
        return [_endpoint(r) for r in rows]

    def endpoint_secret(
        self, tenant_id: str, key_id: str, endpoint_id: str
    ) -> tuple[str, str] | None:
        row = self._conn.execute(
            """SELECT url, secret FROM publishing.webhook_endpoints
               WHERE tenant_id = %s AND api_key_id = %s::uuid AND id = %s::uuid AND active""",
            (tenant_id, key_id, endpoint_id),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def delete_endpoint(self, tenant_id: str, key_id: str, endpoint_id: str) -> bool:
        cursor = self._conn.execute(
            """DELETE FROM publishing.webhook_endpoints
               WHERE tenant_id = %s AND api_key_id = %s::uuid AND id = %s::uuid""",
            (tenant_id, key_id, endpoint_id),
        )
        return cursor.rowcount == 1

    def disable_endpoint(self, endpoint_id: str, reason: str) -> bool:
        cursor = self._conn.execute(
            """UPDATE publishing.webhook_endpoints SET active = false, disabled_reason = %s
               WHERE id = %s::uuid AND active""",
            (reason, endpoint_id),
        )
        return cursor.rowcount == 1

    def candidates(self, tenant_id: str, since: datetime, limit: int) -> list[WebhookCandidate]:
        statuses = [s.value for s in EVENT_FOR_STATUS]
        rows = self._conn.execute(
            """SELECT w.id::text, v.id::text, e.seq, e.to_status, e.at, p.post_key, v.platform,
                      coalesce(a.display_name, v.platform),
                      coalesce(v.snapshot ->> 'format', c.content ->> 'format'),
                      v.status, v.publish_at, v.external_url, e.reason, v.external_id
               FROM publishing.webhook_endpoints w
               JOIN publishing.api_keys k ON k.id = w.api_key_id AND k.revoked_at IS NULL
               JOIN publishing.posts p
                    ON p.tenant_id = w.tenant_id AND p.source = 'agent' AND p.created_by = k.name
               JOIN publishing.variants v ON v.tenant_id = p.tenant_id AND v.post_id = p.id
               JOIN publishing.variant_events e ON e.tenant_id = v.tenant_id AND e.variant_id = v.id
               JOIN publishing.social_accounts a
                    ON a.tenant_id = v.tenant_id AND a.id = v.account_id
               LEFT JOIN publishing.api_content c ON c.variant_id = v.id
               WHERE w.tenant_id = %s AND w.active
                 AND e.at >= %s AND e.at >= w.created_at AND e.to_status = ANY(%s)
                 AND NOT EXISTS (
                     SELECT 1 FROM publishing.webhook_deliveries d
                     WHERE d.endpoint_id = w.id AND d.variant_id = v.id AND d.seq = e.seq)
               ORDER BY e.at, e.seq LIMIT %s""",
            (tenant_id, since, statuses, limit),
        ).fetchall()
        wanted = {e.id: e.events for e in self.endpoints(tenant_id)}
        found = []
        for r in rows:
            event_type = EVENT_FOR_STATUS[VariantStatus(r[3])]
            if wanted[r[0]] and event_type not in wanted[r[0]]:
                continue
            found.append(
                WebhookCandidate(
                    endpoint_id=r[0], variant_id=r[1], seq=r[2], event_type=event_type, at=r[4],
                    post_key=r[5], platform=r[6], account=r[7], format=r[8], status=r[3],
                    slot=r[10], live_url=r[11], reason=r[12],
                    handed_over=str(r[13] or "").startswith("assisted:"),
                )
            )  # fmt: skip
        return found

    def queue(
        self,
        tenant_id: str,
        candidate: WebhookCandidate,
        event_id: str,
        payload: Mapping[str, Any],
        at: datetime,
    ) -> bool:
        row = self._conn.execute(
            """INSERT INTO publishing.webhook_deliveries
                   (tenant_id, endpoint_id, variant_id, seq, event_id, event_type, payload,
                    next_attempt_at)
               VALUES (%s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, endpoint_id, variant_id, seq) DO NOTHING RETURNING 1""",
            (
                tenant_id, candidate.endpoint_id, candidate.variant_id, candidate.seq, event_id,
                candidate.event_type, Jsonb(dict(payload)), at,
            ),
        ).fetchone()  # fmt: skip
        return row is not None

    def claim_due(self, now: datetime, limit: int, lease: timedelta) -> list[WebhookDelivery]:
        rows = self._conn.execute(
            f"""WITH due AS (
                    SELECT id FROM publishing.webhook_deliveries
                    WHERE status = 'pending' AND next_attempt_at <= %s
                    ORDER BY next_attempt_at, id LIMIT %s FOR UPDATE SKIP LOCKED),
                leased AS (
                    UPDATE publishing.webhook_deliveries d SET next_attempt_at = %s
                    FROM due WHERE d.id = due.id RETURNING d.id)
                {_DELIVERY} WHERE d.id IN (SELECT id FROM leased)
                ORDER BY d.created_at""",
            (now, limit, now + lease),
        ).fetchall()
        return [_delivery(r) for r in rows]

    def has_due(self, now: datetime) -> bool:
        return (
            self._conn.execute(
                """SELECT 1 FROM publishing.webhook_deliveries
                   WHERE status = 'pending' AND next_attempt_at <= %s LIMIT 1""",
                (now,),
            ).fetchone()
            is not None
        )

    def record_attempt(
        self,
        delivery_id: str,
        at: datetime,
        *,
        ok: bool,
        http_status: int | None,
        error: str | None,
        retry_at: datetime | None,
    ) -> None:
        status = "delivered" if ok else ("pending" if retry_at is not None else "dead")
        self._conn.execute(
            """UPDATE publishing.webhook_deliveries
               SET status = %s, attempts = attempts + 1, last_status = %s, last_error = %s,
                   next_attempt_at = coalesce(%s, next_attempt_at),
                   delivered_at = CASE WHEN %s THEN %s::timestamptz ELSE delivered_at END
               WHERE id = %s::uuid""",
            (status, http_status, error, retry_at, ok, at, delivery_id),
        )

    def deliveries(
        self, tenant_id: str, key_id: str, endpoint_id: str, limit: int
    ) -> list[WebhookDelivery]:
        rows = self._conn.execute(
            f"""{_DELIVERY} WHERE w.tenant_id = %s AND w.api_key_id = %s::uuid
                  AND d.endpoint_id = %s::uuid ORDER BY d.created_at DESC, d.seq DESC LIMIT %s""",
            (tenant_id, key_id, endpoint_id, limit),
        ).fetchall()
        return [_delivery(r) for r in rows]
