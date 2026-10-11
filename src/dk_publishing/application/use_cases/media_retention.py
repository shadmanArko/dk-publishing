"""Keeping the media disk from filling up (ADR 0021).

Files are deleted only when no post could still need them: every variant that is not final keeps
its files, and so does every variant that became final within the grace period (a failed post is
often fixed and tried again, and an uncertain one is reconciled against its media). Uploads that
no post has used yet are kept for the same grace period. The database keeps every record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from dk_publishing.application.ports import DiskUsage, IntakeRejected, MediaDisk, RemovedFile
from dk_publishing.application.services import ApiServices, Services

GRACE = timedelta(days=7)
WARN_PERCENT = 70  # tell the owner
STOP_PERCENT = 85  # stop accepting uploads
MB = 1024**2


@dataclass(frozen=True, slots=True)
class PurgeResult:
    removed: tuple[RemovedFile, ...]
    dry_run: bool

    @property
    def bytes(self) -> int:
        return sum(f.size for f in self.removed)


def purge_media(
    services: Services,
    disk: MediaDisk,
    *,
    grace: timedelta = GRACE,
    tenant_id: str = "dk",
    dry_run: bool = False,
) -> PurgeResult:
    """Delete media nothing needs any more. Never deletes anything a live post might use."""
    cutoff = services.clock.now() - grace
    with services.uow() as uow:
        keep = list(uow.variants.media_in_use(cutoff))
        keep += [
            {"name": m.filename, "media_id": m.id, "sha256": m.sha256}
            for m in uow.api.media_since(tenant_id, cutoff)
        ]
    removed = disk.delete_unused(keep, cutoff, dry_run=dry_run)
    if removed and not dry_run:
        with services.uow() as uow:
            uow.api.mark_media_purged(
                tenant_id, [f.sha256 for f in removed if f.sha256], services.clock.now()
            )
            uow.commit()
    return PurgeResult(tuple(removed), dry_run)


def disk_warning(usage: DiskUsage) -> str | None:
    """A sentence for the owner when the disk is getting full; the same sentence for each 5%
    step, so the daily reminder does not change wording every day."""
    if usage.percent < WARN_PERCENT:
        return None
    step = usage.percent // 5 * 5
    if usage.percent >= STOP_PERCENT:
        return (
            f"The media disk is over {step}% full: uploads through the API are refused until "
            "space is freed. Remove old files or enlarge the disk."
        )
    return (
        f"The media disk is over {step}% full. Old media is removed automatically after the "
        "posts are done; if this keeps growing, enlarge the disk."
    )


def upload_allowance(api: ApiServices, key_name: str) -> int | None:
    """How many bytes this key may still upload, or None for no extra limit. Raises
    IntakeRejected when the disk is too full or the key has used its quota."""
    if api.disk is not None:
        usage = api.disk.usage()
        if usage.percent >= api.disk_stop_percent:
            raise IntakeRejected(
                "storage_full",
                f"The server's media disk is {usage.percent}% full, so uploads are paused. "
                "Tell the owner; try again after space has been freed.",
            )
    if api.key_quota_bytes <= 0:
        return None
    with api.core.uow() as uow:
        used = uow.api.media_bytes(api.tenant_id, key_name)
    left = api.key_quota_bytes - used
    if left <= 0:
        raise IntakeRejected(
            "quota_exceeded",
            f"This key has {used // MB} MB of uploads on the server, its limit is "
            f"{api.key_quota_bytes // MB} MB. Files are removed a week after their posts are "
            "done; wait for that, or ask the owner to raise the limit.",
        )
    return left
