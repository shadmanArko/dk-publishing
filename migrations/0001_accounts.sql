-- Accounts, their encrypted credentials, and per-platform switches.
-- Conventions (match the AI harness): text not varchar, timestamptz, text+CHECK never ENUM,
-- uuid PKs, explicit constraint names (pk_ uq_ fk_ ck_ idx_). Every table carries tenant_id and
-- every unique key includes it. Platform names are deliberately NOT in a CHECK: adding a platform
-- must cost an adapter, a Sheet tab and a config entry, never a migration.

CREATE SCHEMA IF NOT EXISTS publishing;

CREATE TABLE publishing.social_accounts (
    id           uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL,
    platform     text        NOT NULL,
    external_id  text        NOT NULL,
    display_name text,
    status       text        NOT NULL DEFAULT 'active',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_social_accounts PRIMARY KEY (id),
    CONSTRAINT uq_social_accounts_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_social_accounts_tenant_platform_external UNIQUE (tenant_id, platform, external_id),
    CONSTRAINT ck_social_accounts_status CHECK (status IN ('active', 'needs_reauth', 'paused')),
    CONSTRAINT ck_social_accounts_platform CHECK (btrim(platform) <> '')
);

-- Tokens are AES-256-GCM ciphertext. Only the vault adapter's database role will be granted
-- access (roles are cluster-level, so they are created at deploy time, not here).
CREATE TABLE publishing.credentials (
    tenant_id          text        NOT NULL,
    account_id         uuid        NOT NULL,
    ciphertext         bytea       NOT NULL,
    key_version        integer     NOT NULL,
    access_expires_at  timestamptz,
    refresh_expires_at timestamptz,
    scopes             text[]      NOT NULL DEFAULT '{}',
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_credentials PRIMARY KEY (account_id),
    CONSTRAINT fk_credentials_account FOREIGN KEY (tenant_id, account_id)
        REFERENCES publishing.social_accounts (tenant_id, id),
    CONSTRAINT ck_credentials_key_version CHECK (key_version >= 1)
);

CREATE TABLE publishing.channel_settings (
    tenant_id  text        NOT NULL,
    platform   text        NOT NULL,
    mode       text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_channel_settings PRIMARY KEY (tenant_id, platform),
    CONSTRAINT ck_channel_settings_mode CHECK (mode IN ('off', 'dry_run', 'assisted', 'live'))
);
