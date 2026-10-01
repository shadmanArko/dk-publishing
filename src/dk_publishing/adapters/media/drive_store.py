"""Download approved media from Google Drive to local disk, verified against its checksum."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from dk_publishing.domain.errors import Rejected, Retryable
from dk_publishing.domain.publishing import Rendition

CHUNK = 8 * 1024 * 1024


class DriveDownloader(Protocol):
    def download(self, file_id: str, sink: BinaryIO) -> None: ...


class GoogleDriveDownloader:
    def __init__(self, drive: Any) -> None:
        self._drive = drive

    def download(self, file_id: str, sink: BinaryIO) -> None:
        request = self._drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(sink, request, chunksize=CHUNK)
        try:
            done = False
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status in (403, 404):
                raise Rejected("the file is no longer reachable in Drive") from None
            raise Retryable(f"Drive download failed (HTTP {status})") from None


class _HashingSink:
    """Writes to a file while computing its checksums in the same pass."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self.md5 = hashlib.md5()
        self.sha256 = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.md5.update(data)
        self.sha256.update(data)
        return self._handle.write(data)


class DriveMediaStore:
    def __init__(self, downloader: DriveDownloader, base_dir: Path) -> None:
        self._downloader = downloader
        self._dir = base_dir / "originals"

    def ensure_local(self, media: Sequence[Mapping[str, Any]]) -> list[Rendition]:
        return [self._one(item) for item in media]

    def _one(self, item: Mapping[str, Any]) -> Rendition:
        name, file_id, md5 = str(item.get("name", "")), item.get("drive_file_id"), item.get("md5")
        if not file_id:
            raise Rejected(f"'{name}' was not found in the Drive folder")
        self._dir.mkdir(parents=True, exist_ok=True)
        # Named by checksum, so a replaced file never collides with the old one, and a name like
        # '../../x' can never leave the folder.
        dest = self._dir / f"{md5 or file_id}-{_safe(name)}"
        if dest.exists():
            return Rendition("original", str(dest), _sha256(dest))

        part = dest.with_name(dest.name + ".part")
        try:
            with open(part, "wb") as handle:
                sink = _HashingSink(handle)
                self._downloader.download(str(file_id), sink)  # type: ignore[arg-type]
            if md5 and sink.md5.hexdigest() != md5:
                raise Retryable(
                    f"'{name}' changed in Drive after it was approved; the next sync will notice"
                )
            part.rename(dest)
        finally:
            part.unlink(missing_ok=True)
        return Rendition("original", str(dest), sink.sha256.hexdigest())


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name) or "file"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()
