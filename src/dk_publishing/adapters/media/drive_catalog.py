from __future__ import annotations

from typing import Any

from dk_publishing.domain.sheet import MediaFile

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveCatalog:
    """The files in the Drive media folder, with their checksums."""

    def __init__(self, drive: Any, folder_id: str) -> None:
        self._drive = drive
        self._folder_id = folder_id

    def list_files(self) -> list[MediaFile]:
        files: list[MediaFile] = []
        token: str | None = None
        while True:
            page = (
                self._drive.files()
                .list(
                    q=f"'{self._folder_id}' in parents and trashed = false",
                    fields="nextPageToken,files(id,name,md5Checksum,mimeType,size)",
                    pageSize=200,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for f in page.get("files", []):
                if f.get("mimeType") == FOLDER_MIME:
                    continue
                files.append(
                    MediaFile(
                        id=f["id"],
                        name=f["name"],
                        md5=f.get("md5Checksum"),
                        mime=f.get("mimeType"),
                        size=int(f["size"]) if f.get("size") else None,
                    )
                )
            token = page.get("nextPageToken")
            if not token:
                return files


def sheet_modified_time(drive: Any, sheet_id: str) -> str:
    """Drive's modifiedTime for the spreadsheet: it changes on every edit, including ours."""
    info = (
        drive.files().get(fileId=sheet_id, fields="modifiedTime", supportsAllDrives=True).execute()
    )
    return str(info["modifiedTime"])
