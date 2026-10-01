-- Support for the Sheet sync (expand-only: nothing here breaks the previous release).

-- Hash of everything a person can edit that affects this variant (its row, the Posts row it hangs
-- off, and the Drive files' checksums). A change after approval withdraws the approval.
ALTER TABLE publishing.variants ADD COLUMN source_hash text;

-- The Sheet's `account` dropdown holds display names, so within a platform they must be unique.
CREATE UNIQUE INDEX uq_social_accounts_display_name
    ON publishing.social_accounts (tenant_id, platform, lower(display_name))
    WHERE display_name IS NOT NULL;
