-- What the dry-run publisher "posted". Durable on purpose: each Dagster run is its own process,
-- so a reconcile in one run must see what a publish in another run did, exactly as it would
-- with a real platform. Deliberately no unique key on variant_id: a double publish must be
-- visible here, not silently collapsed.

CREATE TABLE publishing.dry_run_posts (
    id          uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id   text        NOT NULL,
    variant_id  uuid        NOT NULL,
    platform    text        NOT NULL,
    external_id text        NOT NULL,
    payload     jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_dry_run_posts PRIMARY KEY (id),
    CONSTRAINT uq_dry_run_posts_tenant_external UNIQUE (tenant_id, external_id),
    CONSTRAINT fk_dry_run_posts_variant FOREIGN KEY (tenant_id, variant_id)
        REFERENCES publishing.variants (tenant_id, id)
);

CREATE INDEX idx_dry_run_posts_variant ON publishing.dry_run_posts (variant_id);
