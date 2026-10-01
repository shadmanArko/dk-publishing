-- Variants and the append-only trail around them. The scheduler reads one indexed column:
-- variants.next_action_at.

CREATE TABLE publishing.variants (
    id             uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id      text        NOT NULL,
    post_id        uuid        NOT NULL,
    platform       text        NOT NULL,
    account_id     uuid        NOT NULL,
    status         text        NOT NULL DEFAULT 'draft',
    version        integer     NOT NULL DEFAULT 0,
    publish_at     timestamptz NOT NULL,
    snapshot       jsonb,
    snapshot_hash  text,
    next_action    text,
    next_action_at timestamptz,
    native_handle  jsonb,
    external_id    text,
    external_url   text,
    published_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_variants PRIMARY KEY (id),
    CONSTRAINT uq_variants_tenant_id UNIQUE (tenant_id, id),
    CONSTRAINT uq_variants_tenant_post_platform_account UNIQUE (tenant_id, post_id, platform, account_id),
    CONSTRAINT fk_variants_post FOREIGN KEY (tenant_id, post_id)
        REFERENCES publishing.posts (tenant_id, id),
    CONSTRAINT fk_variants_account FOREIGN KEY (tenant_id, account_id)
        REFERENCES publishing.social_accounts (tenant_id, id),
    CONSTRAINT ck_variants_platform CHECK (btrim(platform) <> ''),
    CONSTRAINT ck_variants_status CHECK (status IN (
        'draft', 'invalid', 'pending_approval', 'approved', 'preparing', 'prepared',
        'scheduling_native', 'scheduled_native', 'publishing', 'unknown', 'published',
        'failed', 'cancelled', 'expired')),
    CONSTRAINT ck_variants_version CHECK (version >= 0),
    CONSTRAINT ck_variants_next_action CHECK (next_action IS NULL OR next_action IN (
        'fetch_media', 'schedule_native', 'prepare', 'publish', 'reconcile', 'expire')),
    CONSTRAINT ck_variants_next_action_pair CHECK ((next_action IS NULL) = (next_action_at IS NULL)),
    -- Backstops for the domain rules, so a bug or a hand-written UPDATE cannot create an
    -- approved-looking variant without a frozen snapshot, or a published one without a time.
    CONSTRAINT ck_variants_snapshot_pair CHECK ((snapshot IS NULL) = (snapshot_hash IS NULL)),
    CONSTRAINT ck_variants_snapshot_when_approved CHECK (
        snapshot_hash IS NOT NULL OR status IN (
            'draft', 'invalid', 'pending_approval', 'failed', 'cancelled', 'expired')),
    CONSTRAINT ck_variants_published_at CHECK ((status = 'published') = (published_at IS NOT NULL))
);

-- Only rows with work to do are indexed, so the scheduler's query stays fast however much
-- history accumulates.
CREATE INDEX idx_variants_due ON publishing.variants (next_action_at) WHERE next_action_at IS NOT NULL;

CREATE TABLE publishing.variant_media (
    tenant_id  text    NOT NULL,
    variant_id uuid    NOT NULL,
    position   integer NOT NULL,
    asset_id   uuid    NOT NULL,
    CONSTRAINT pk_variant_media PRIMARY KEY (variant_id, position),
    CONSTRAINT fk_variant_media_variant FOREIGN KEY (tenant_id, variant_id)
        REFERENCES publishing.variants (tenant_id, id),
    CONSTRAINT fk_variant_media_asset FOREIGN KEY (tenant_id, asset_id)
        REFERENCES publishing.media_assets (tenant_id, id),
    CONSTRAINT ck_variant_media_position CHECK (position >= 0)
);

CREATE TABLE publishing.variant_events (
    tenant_id   text        NOT NULL,
    variant_id  uuid        NOT NULL,
    seq         integer     NOT NULL,
    from_status text        NOT NULL,
    to_status   text        NOT NULL,
    actor_kind  text        NOT NULL,
    actor_name  text        NOT NULL,
    reason      text        NOT NULL,
    at          timestamptz NOT NULL,
    CONSTRAINT pk_variant_events PRIMARY KEY (variant_id, seq),
    CONSTRAINT fk_variant_events_variant FOREIGN KEY (tenant_id, variant_id)
        REFERENCES publishing.variants (tenant_id, id),
    CONSTRAINT ck_variant_events_seq CHECK (seq >= 1),
    CONSTRAINT ck_variant_events_actor_kind CHECK (actor_kind IN ('human', 'system', 'agent')),
    CONSTRAINT ck_variant_events_reason CHECK (btrim(reason) <> '')
);

CREATE TABLE publishing.publish_attempts (
    id               uuid        NOT NULL DEFAULT gen_random_uuid(),
    tenant_id        text        NOT NULL,
    variant_id       uuid        NOT NULL,
    phase            text        NOT NULL,
    idempotency_key  text        NOT NULL,
    outcome          text,
    error_code       text,
    http_status      integer,
    response_excerpt text,
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    CONSTRAINT pk_publish_attempts PRIMARY KEY (id),
    CONSTRAINT uq_publish_attempts_tenant_key UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT fk_publish_attempts_variant FOREIGN KEY (tenant_id, variant_id)
        REFERENCES publishing.variants (tenant_id, id),
    CONSTRAINT ck_publish_attempts_phase CHECK (
        phase IN ('prepare', 'schedule_native', 'publish', 'reconcile')),
    CONSTRAINT ck_publish_attempts_outcome CHECK (outcome IS NULL OR outcome IN (
        'ok', 'retryable', 'rate_limited', 'auth_failed', 'rejected', 'unknown')),
    CONSTRAINT ck_publish_attempts_finished CHECK ((outcome IS NULL) = (finished_at IS NULL)),
    CONSTRAINT ck_publish_attempts_excerpt CHECK (char_length(response_excerpt) <= 2000)
);

CREATE INDEX idx_publish_attempts_variant ON publishing.publish_attempts (variant_id);

-- Append-only enforcement ---------------------------------------------------------------------

CREATE FUNCTION publishing.forbid_modification() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER trg_variant_events_append_only
    BEFORE UPDATE OR DELETE ON publishing.variant_events
    FOR EACH ROW EXECUTE FUNCTION publishing.forbid_modification();

CREATE TRIGGER trg_sheet_snapshots_append_only
    BEFORE UPDATE OR DELETE ON publishing.sheet_snapshots
    FOR EACH ROW EXECUTE FUNCTION publishing.forbid_modification();

CREATE TRIGGER trg_publish_attempts_no_delete
    BEFORE DELETE ON publishing.publish_attempts
    FOR EACH ROW EXECUTE FUNCTION publishing.forbid_modification();

-- An attempt is written before the platform call (intent log) and completed once afterwards.
-- A completed attempt never changes, and its identity columns never change at all.
CREATE FUNCTION publishing.guard_attempt_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'publish_attempts % is already finished', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.id, NEW.tenant_id, NEW.variant_id, NEW.phase, NEW.idempotency_key, NEW.started_at)
       IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.variant_id, OLD.phase, OLD.idempotency_key, OLD.started_at) THEN
        RAISE EXCEPTION 'publish_attempts % identity columns are immutable', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_publish_attempts_guard_update
    BEFORE UPDATE ON publishing.publish_attempts
    FOR EACH ROW EXECUTE FUNCTION publishing.guard_attempt_update();

-- "No unapproved posts": a variant can only get a `published` event after an `approved` one.
CREATE FUNCTION publishing.require_approval_before_published() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.to_status = 'published' AND NOT EXISTS (
        SELECT 1 FROM publishing.variant_events e
        WHERE e.variant_id = NEW.variant_id AND e.to_status = 'approved'
    ) THEN
        RAISE EXCEPTION 'variant % cannot be published: it was never approved', NEW.variant_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_variant_events_approved_before_published
    BEFORE INSERT ON publishing.variant_events
    FOR EACH ROW EXECUTE FUNCTION publishing.require_approval_before_published();
