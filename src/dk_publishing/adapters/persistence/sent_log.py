from __future__ import annotations

import psycopg


class PostgresSentLog:
    """Which hand-over cards were already sent, in the same table as the alerts. Its own short
    connection per call: the publisher must not hold a transaction while it talks to Telegram."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        _, tenant, _ = key.split(":", 2)  # assist:<tenant>:<variant>
        return tenant, key

    def was_sent(self, key: str) -> bool:
        tenant, key = self._split(key)
        with psycopg.connect(self._conninfo) as conn:
            row = conn.execute(
                "SELECT 1 FROM publishing.alerts_sent WHERE tenant_id = %s AND key = %s",
                (tenant, key),
            ).fetchone()
        return row is not None

    def mark_sent(self, key: str) -> None:
        tenant, key = self._split(key)
        with psycopg.connect(self._conninfo) as conn:
            conn.execute(
                """INSERT INTO publishing.alerts_sent (tenant_id, key, sent_at)
                   VALUES (%s, %s, now()) ON CONFLICT DO NOTHING""",
                (tenant, key),
            )
