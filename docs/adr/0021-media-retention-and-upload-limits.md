# ADR 0021: Media is deleted when no post needs it, and uploads have limits

Status: accepted

## Context

API uploads (`<media>/uploads`) and Drive downloads (`<media>/originals`) were never deleted. The VPS disk is
small and shared with the ordering system, so disk use could only grow until it took both down.

## Decision

- **A nightly job (`media_cleanup`, 04:15 Berlin; `dk media purge [--dry-run]` by hand)** deletes a media file when
  no post can still need it. A file is kept while any variant that is not final references it (this includes API
  drafts, whose content is in `api_content`), while any variant became final less than 7 days ago
  (`MEDIA_RETENTION_DAYS`), and while it is an upload younger than 7 days that no post has used yet.
  `unknown` and `failed` count as not final or recent on purpose: reconcile and retries read the files.
- **Records stay.** `api_media.purged_at` is set. A post that names a removed file is told `media_expired` and
  must upload it again. A Sheet post whose file was removed is simply downloaded from Drive again at approval.
- **Disk guard.** Uploads are refused with 507 `storage_full` at 85% disk use (`MEDIA_DISK_STOP_PERCENT`).
  The nightly job warns on Telegram from 70% (once a day per 5% step, through `report_health`).
- **Quota.** One key may keep 5 GB on the server (`API_KEY_QUOTA_MB`, 0 = unlimited): 413 `quota_exceeded`. The
  per-file cap dropped from 1 GB to 512 MB (`API_MAX_UPLOAD_MB`), and a file may not exceed what is left of the quota.

## Consequences

- The janitor matches files to posts by name (`<sha256>-<name>`, `<md5>-<name>`), the same rule the stores use to
  write them. If either naming rule changes, change `adapters/media/janitor.py` with it.
- Deleting too early is the risk, hence the grace period and the "not final" rule. Disk-full is still possible if
  a week of live posts is larger than the disk; the guard then refuses uploads instead of failing posts.
