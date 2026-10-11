-- Webhooks (ADR 0023): the server tells an API client when its posts change state.
-- Expand-only. An endpoint belongs to one API key; a delivery is one event for one endpoint.

CREATE TABLE publishing.webhook_endpoints (
    id              uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       text        NOT NULL,
    api_key_id      uuid        NOT NULL,
    url             text        NOT NULL,
    -- The signing secret has to be known to sign, so it cannot be a hash like an API key. It is shown
    -- once at creation; a leak of this table would let someone forge messages to that one client.
    secret          text        NOT NULL,
    events          text[]      NOT NULL DEFAULT '{}',   -- empty: every event
    active          boolean     NOT NULL DEFAULT true,
    disabled_reason text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_webhook_endpoints PRIMARY KEY (id),
    CONSTRAINT uq_webhook_endpoints_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT fk_webhook_endpoints_key FOREIGN KEY (api_key_id) REFERENCES publishing.api_keys (id),
    CONSTRAINT ck_webhook_endpoints_url CHECK (url LIKE 'https://%' AND length(url) <= 2000)
);
CREATE INDEX idx_webhook_endpoints_key ON publishing.webhook_endpoints (api_key_id);

CREATE TABLE publishing.webhook_deliveries (
    id              uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       text        NOT NULL,
    endpoint_id     uuid        NOT NULL,
    variant_id      uuid        NOT NULL,
    seq             integer     NOT NULL,           -- the variant_events row this message reports
    event_id        text        NOT NULL,
    event_type      text        NOT NULL,
    payload         jsonb       NOT NULL,
    status          text        NOT NULL DEFAULT 'pending',
    attempts        integer     NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL,
    last_status     integer,                         -- the HTTP status the client answered with
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz,
    CONSTRAINT pk_webhook_deliveries PRIMARY KEY (id),
    -- One message per event and endpoint, however often the producer runs.
    CONSTRAINT uq_webhook_deliveries_event UNIQUE (tenant_id, endpoint_id, variant_id, seq),
    CONSTRAINT fk_webhook_deliveries_endpoint FOREIGN KEY (endpoint_id)
        REFERENCES publishing.webhook_endpoints (id) ON DELETE CASCADE,
    CONSTRAINT ck_webhook_deliveries_status CHECK (status IN ('pending', 'delivered', 'dead')),
    CONSTRAINT ck_webhook_deliveries_attempts CHECK (attempts >= 0)
);
-- Only waiting messages are indexed, so the "what is due" query stays fast as history grows.
CREATE INDEX idx_webhook_deliveries_due ON publishing.webhook_deliveries (next_attempt_at)
    WHERE status = 'pending';
CREATE INDEX idx_webhook_deliveries_endpoint ON publishing.webhook_deliveries (endpoint_id, created_at DESC);
