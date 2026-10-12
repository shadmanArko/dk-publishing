"""Register a file that was sent through the API, so posts can refer to it by id."""

from __future__ import annotations

from dk_publishing.application.ports import StoredFile, UploadedMedia
from dk_publishing.application.services import ApiServices


def register_media(api: ApiServices, stored: StoredFile, *, created_by: str) -> UploadedMedia:
    with api.core.uow() as uow:
        media_id = uow.api.add_media(
            api.tenant_id, stored.filename, stored.sha256, stored.size, created_by
        )
        uow.commit()
    return UploadedMedia(media_id, stored.filename, stored.sha256, stored.size)


def fetch_media(
    api: ApiServices, url: str, filename: str | None, *, created_by: str
) -> UploadedMedia:
    """Download a public https file within the key's allowance and register it. Raises
    IntakeRejected (a refused link, type, size, quota or a full disk)."""
    from dk_publishing.application.use_cases.media_retention import upload_allowance

    stored = api.intake.fetch(url, filename, upload_allowance(api, created_by))
    return register_media(api, stored, created_by=created_by)
