"""The media disk: how full it is, and removing files nothing needs any more.

Two folders hold files: `uploads/` (sent through the API, `<sha256>-<name>`) and `originals/`
(downloaded from Drive, `<md5 or file id>-<name>`). A file is kept when some media item that is
still needed would resolve to its name; the same naming rules as the stores that wrote them.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dk_publishing.application.ports import DiskUsage, RemovedFile

UPLOADS, ORIGINALS = "uploads", "originals"
PART_AGE = timedelta(hours=24)  # a half-written file older than this was abandoned
SHA256 = re.compile(r"^[0-9a-f]{64}-")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name) or "file"


def _wanted(keep: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """The file names (uploads, originals) that the items still need."""
    uploads: set[str] = set()
    originals: set[str] = set()
    for item in keep:
        name = _safe(str(item.get("name", "")))
        if item.get("media_id"):
            uploads.add(f"{item.get('sha256', '')}-{name}")
        else:
            originals.add(f"{item.get('md5') or item.get('drive_file_id') or ''}-{name}")
    return uploads, originals


class MediaDiskJanitor:
    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir

    def usage(self) -> DiskUsage:
        probe = self._base
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        total, used, _ = shutil.disk_usage(probe)
        return DiskUsage(total=total, used=used)

    def delete_unused(
        self, keep: Sequence[Mapping[str, Any]], modified_before: datetime, *, dry_run: bool = False
    ) -> list[RemovedFile]:
        uploads, originals = _wanted(keep)
        removed: list[RemovedFile] = []
        for folder, wanted in ((UPLOADS, uploads), (ORIGINALS, originals)):
            directory = self._base / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.is_symlink():
                    continue
                stat = path.stat()
                touched = datetime.fromtimestamp(stat.st_mtime, UTC)
                if path.name.endswith(".part"):
                    stale = touched < datetime.now(UTC) - PART_AGE
                    if not stale:
                        continue
                elif path.name in wanted or touched >= modified_before:
                    continue
                sha = path.name[:64] if folder == UPLOADS and SHA256.match(path.name) else None
                if not dry_run:
                    path.unlink(missing_ok=True)
                removed.append(RemovedFile(f"{folder}/{path.name}", stat.st_size, sha))
        return removed
