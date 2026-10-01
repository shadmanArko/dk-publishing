-- Dry-run support for natively scheduled posts: a "post" the fake platform holds until a time,
-- and can be told to drop. Expand-only.
ALTER TABLE publishing.dry_run_posts
    ADD COLUMN scheduled_for timestamptz,
    ADD COLUMN cancelled_at  timestamptz;
