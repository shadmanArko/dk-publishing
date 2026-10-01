-- Posts, raw Sheet snapshots, and media.

CREATE TABLE publishing.posts (
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id  text        NOT NULL,
    post_key   text        NOT NULL,
    title      text,
    source     text        NOT NULL DEFAULT 'human',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_posts PRIMARY KEY (id),
    CONSTRAINT uq_posts_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_posts_tenant_post_key UNIQUE (tenant_id, post_key),
    CONSTRAINT ck_posts_source CHECK (source IN ('human', 'agent'))
);

-- Append-only record of every sync that changed something. `cells` rather than `values`:
-- VALUES is a reserved word and would need quoting in every query forever.
CREATE TABLE publishing.sheet_snapshots (
    tenant_id  text        NOT NULL,
    sync_id    uuid        NOT NULL,
    tab        text        NOT NULL,
    row_index  integer     NOT NULL,
    cells      jsonb       NOT NULL,
    row_hash   text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_sheet_snapshots PRIMARY KEY (sync_id, tab, row_index),
    CONSTRAINT ck_sheet_snapshots_row_index CHECK (row_index >= 1)
);

CREATE TABLE publishing.media_assets (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     text        NOT NULL,
    sha256        text        NOT NULL,
    drive_file_id text,
    drive_md5     text,
    mime          text        NOT NULL,
    bytes         bigint      NOT NULL,
    width         integer,
    height        integer,
    duration_ms   integer,
    purged_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_media_assets PRIMARY KEY (id),
    CONSTRAINT uq_media_assets_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_media_assets_tenant_sha256 UNIQUE (tenant_id, sha256),
    CONSTRAINT ck_media_assets_bytes CHECK (bytes >= 0)
);

CREATE TABLE publishing.renditions (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id     text        NOT NULL,
    asset_id      uuid        NOT NULL,
    profile       text        NOT NULL,
    path          text        NOT NULL,
    sha256        text        NOT NULL,
    public_token  text,
    exposed_until timestamptz,
    purged_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_renditions PRIMARY KEY (id),
    CONSTRAINT uq_renditions_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_renditions_tenant_asset_profile UNIQUE (tenant_id, asset_id, profile),
    CONSTRAINT fk_renditions_asset FOREIGN KEY (tenant_id, asset_id)
        REFERENCES publishing.media_assets (tenant_id, id),
    CONSTRAINT ck_renditions_exposure CHECK ((public_token IS NULL) = (exposed_until IS NULL))
);

-- A public token is a bearer secret in a URL, so it must be globally unique.
CREATE UNIQUE INDEX uq_renditions_public_token
    ON publishing.renditions (public_token) WHERE public_token IS NOT NULL;
