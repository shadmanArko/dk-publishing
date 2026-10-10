-- The HTTP API: client keys, uploaded media, and the content of posts submitted through it.
-- Expand-only. Posts made through the API have posts.source = 'agent' (the column already allows it);
-- the Sheet sync leaves those alone.

-- Only the SHA-256 of a key is stored: a database leak must not leak working keys.
CREATE TABLE publishing.api_keys (
    id           uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL,
    name         text        NOT NULL,
    key_hash     text        NOT NULL,
    scopes       text[]      NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz,
    CONSTRAINT pk_api_keys PRIMARY KEY (id),
    CONSTRAINT uq_api_keys_tenant_hash UNIQUE (tenant_id, key_hash),
    CONSTRAINT ck_api_keys_name CHECK (btrim(name) <> '')
);
-- Looking a key up starts from the key alone (the tenant comes from the row it finds).
CREATE INDEX idx_api_keys_hash ON publishing.api_keys (key_hash);
-- A name may be reused only after the old key is revoked.
CREATE UNIQUE INDEX uq_api_keys_live_name ON publishing.api_keys (tenant_id, name)
    WHERE revoked_at IS NULL;

CREATE TABLE publishing.api_media (
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id  text        NOT NULL,
    filename   text        NOT NULL,
    sha256     text        NOT NULL,
    bytes      bigint      NOT NULL,
    created_by text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_api_media PRIMARY KEY (id),
    CONSTRAINT uq_api_media_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT ck_api_media_bytes CHECK (bytes >= 0)
);

-- A draft has no frozen snapshot yet, so the content a client submitted is kept here until the
-- variant is approved (approval freezes and hashes it as usual).
CREATE TABLE publishing.api_content (
    variant_id uuid  NOT NULL,
    tenant_id  text  NOT NULL,
    content    jsonb NOT NULL,
    CONSTRAINT pk_api_content PRIMARY KEY (variant_id),
    CONSTRAINT fk_api_content_variant FOREIGN KEY (tenant_id, variant_id)
        REFERENCES publishing.variants (tenant_id, id)
);

-- What was submitted, hashed: the same post_key with the same request is a safe repeat, the same
-- key with a different request is a conflict.
ALTER TABLE publishing.posts ADD COLUMN request_hash text;
ALTER TABLE publishing.posts ADD COLUMN created_by text;
