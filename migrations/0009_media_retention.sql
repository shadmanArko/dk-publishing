-- Uploaded files are removed from the disk after their posts are finished (ADR 0021). The record
-- stays, so a post that still names the file can be told what happened to it.
ALTER TABLE publishing.api_media ADD COLUMN purged_at timestamptz;
CREATE INDEX idx_api_media_created_by ON publishing.api_media (tenant_id, created_by)
    WHERE purged_at IS NULL;
