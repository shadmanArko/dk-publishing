"""Showing a draft's media to the owner, with the tokenised public links the platforms use.

The review page needs a link the browser can play. The server already makes exactly that for the
platforms that fetch media from a URL (`PublicMediaStore`: an unguessable token folder holding a
shortcut to the file, served read-only by the web server and swept after 24 hours), so the page
reuses it instead of adding a second way to serve files. A link is kept for a few hours and handed
out again on every reload, so ten reloads do not leave ten folders behind.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from dk_publishing.adapters.media.public import PublicMedia
from dk_publishing.application.ports import MediaStore
from dk_publishing.domain.errors import Rejected

KEEP = timedelta(hours=12)  # well inside the 24 h sweep of unrevoked links


class PublicLinkPreview:
    def __init__(
        self,
        uploads: MediaStore,
        public: PublicMedia,
        now: Callable[[], datetime],
        keep: timedelta = KEEP,
    ) -> None:
        self._uploads = uploads
        self._public = public
        self._now = now
        self._keep = keep
        self._links: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def url_for(self, item: Mapping[str, Any]) -> str | None:
        """A link for an uploaded file; None for anything else, or a file already removed."""
        if not item.get("media_id"):
            return None  # only files agents upload are previewed; Drive files belong to the Sheet
        key = (str(item.get("sha256") or ""), str(item.get("name") or ""))
        now = self._now()
        with self._lock:
            known = self._links.get(key)
            if known is not None and now - known[1] < self._keep:
                return known[0]
            try:
                [rendition] = self._uploads.ensure_local([item])
                url = self._public.expose(rendition)
            except (Rejected, OSError):
                return None  # the file was cleaned up, or the link folder is not writable
            self._links[key] = (url, now)
            return url
