"""Temporary public links to media, for platforms that fetch media from a URL themselves.

Each file is exposed under an unguessable random token, served read-only by the web server
(Caddy) from `directory`, and removed again after use. Nothing here makes the original file public.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse

from dk_publishing.domain.publishing import Rendition


class PublicMedia(Protocol):
    def expose(self, rendition: Rendition) -> str:
        """A public URL for the file. Anyone with the link can fetch it until `revoke`."""

    def revoke(self, url: str) -> None: ...


class PublicMediaStore:
    def __init__(self, directory: Path, base_url: str) -> None:
        self._dir = directory
        self._base = base_url.rstrip("/")

    def expose(self, rendition: Rendition) -> str:
        token = secrets.token_urlsafe(24)
        folder = self._dir / token
        folder.mkdir(parents=True, exist_ok=False, mode=0o755)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(rendition.path).name) or "file"
        target = folder / name
        try:
            os.symlink(rendition.path, target)  # the original is never moved or copied
        except OSError:
            shutil.copy2(rendition.path, target)
        return f"{self._base}/{token}/{quote(name)}"

    def revoke(self, url: str) -> None:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if len(parts) < 2:
            return
        folder = (self._dir / parts[-2]).resolve()
        if folder.parent == self._dir.resolve():  # never delete anything outside the folder
            shutil.rmtree(folder, ignore_errors=True)

    def purge_older_than(self, age: timedelta, now: datetime | None = None) -> int:
        """Remove links nobody revoked (a run that died). Returns how many were removed."""
        cutoff = (now or datetime.now(UTC)) - age
        removed = 0
        if not self._dir.exists():
            return 0
        for folder in self._dir.iterdir():
            if folder.is_dir() and datetime.fromtimestamp(folder.stat().st_mtime, UTC) < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
