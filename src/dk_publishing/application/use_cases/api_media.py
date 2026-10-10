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
