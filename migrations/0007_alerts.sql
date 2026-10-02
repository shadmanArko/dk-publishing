-- Which alerts and digests were already delivered, so a retry never repeats one and a failed
-- delivery is simply tried again. The key says what the message was about, e.g.
-- 'event:<variant>:<seq>', 'digest:2026-11-14', 'run:<dagster run>'. Expand-only.
CREATE TABLE publishing.alerts_sent (
    tenant_id text        NOT NULL,
    key       text        NOT NULL,
    sent_at   timestamptz NOT NULL,
    CONSTRAINT pk_alerts_sent PRIMARY KEY (tenant_id, key),
    CONSTRAINT ck_alerts_sent_key CHECK (btrim(key) <> '')
);
