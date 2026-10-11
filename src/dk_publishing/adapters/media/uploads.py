"""Files sent through the API: kept on the media disk, named by their checksum.

Two ways in: the client sends the bytes, or gives a public https link we download. Either way the
file ends up as `<sha256>-<name>` under `<media dir>/uploads`, so identical files share one copy,
a name like `../../x` can never leave the folder, and what was approved is exactly what is posted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from dk_publishing.application.ports import IntakeRejected, MediaStore, StoredFile, UploadWriter
from dk_publishing.domain.errors import Rejected
from dk_publishing.domain.publishing import Rendition

# Every file type some platform accepts. Whether a given platform takes it is checked per post.
ALLOWED = (".jpg", ".jpeg", ".png", ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv")
DEFAULT_MAX_BYTES = 512 * 1024**2
MAX_REDIRECTS = 3
Resolver = Callable[[str], Sequence[str]]


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name) or "file"


def resolve_host(host: str) -> list[str]:
    try:
        return sorted(
            {str(i[4][0]) for i in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        )
    except OSError:
        return []


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


class _Writer:
    def __init__(self, store: UploadStore, filename: str, limit: int | None = None) -> None:
        self._store = store
        self._name = filename
        self._limit = min(limit, store.max_bytes) if limit else store.max_bytes
        store.dir.mkdir(parents=True, exist_ok=True)
        self._part = store.dir / f".{safe_name(filename)}.{id(self)}.part"
        self._handle = open(self._part, "wb")  # noqa: SIM115 - closed in finish/abort
        self._sha = hashlib.sha256()
        self._size = 0

    def write(self, chunk: bytes) -> None:
        self._size += len(chunk)
        if self._size > self._limit:
            self.abort()
            raise IntakeRejected(
                "too_large", f"The file is over the {self._limit // 1024**2} MB limit."
            )
        self._sha.update(chunk)
        self._handle.write(chunk)

    def finish(self) -> StoredFile:
        self._handle.close()
        if self._size == 0:
            self._part.unlink(missing_ok=True)
            raise IntakeRejected("empty", "The file is empty.")
        digest = self._sha.hexdigest()
        name = safe_name(self._name)
        dest = self._store.dir / f"{digest}-{name}"
        if dest.exists():
            self._part.unlink(missing_ok=True)
        else:
            self._part.rename(dest)
        return StoredFile(name, digest, self._size)

    def abort(self) -> None:
        self._handle.close()
        self._part.unlink(missing_ok=True)


class UploadStore:
    def __init__(
        self,
        base_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = resolve_host,
    ) -> None:
        self.dir = base_dir / "uploads"
        self.max_bytes = max_bytes
        self._transport = transport
        self._resolver = resolver

    # --- MediaIntake ---------------------------------------------------------------------

    def begin(self, filename: str, limit: int | None = None) -> UploadWriter:
        self._check_type(filename)
        return _Writer(self, filename, limit)

    def fetch(self, url: str, filename: str | None, limit: int | None = None) -> StoredFile:
        response_name = filename
        current = url
        with httpx.Client(transport=self._transport, timeout=60.0, follow_redirects=False) as http:
            for _ in range(MAX_REDIRECTS + 1):
                self._check_url(current)
                try:
                    with http.stream("GET", current) as response:
                        if response.is_redirect:
                            target = response.headers.get("location", "")
                            current = urljoin(current, target)
                            continue
                        if response.status_code != 200:
                            raise IntakeRejected(
                                "fetch_failed",
                                f"The link answered HTTP {response.status_code}, not 200.",
                            )
                        name = response_name or unquote(Path(urlparse(current).path).name)
                        writer = self.begin(name or "file", limit)
                        try:
                            for chunk in response.iter_bytes(1024 * 1024):
                                writer.write(chunk)
                            return writer.finish()
                        except BaseException:
                            writer.abort()
                            raise
                except httpx.HTTPError:
                    raise IntakeRejected(
                        "fetch_failed", "The link could not be downloaded."
                    ) from None
        raise IntakeRejected(
            "fetch_failed", f"The link redirected more than {MAX_REDIRECTS} times."
        )

    # --- MediaStore ----------------------------------------------------------------------

    def ensure_local(self, media: Sequence[Mapping[str, Any]]) -> list[Rendition]:
        renditions: list[Rendition] = []
        for item in media:
            digest, name = str(item.get("sha256", "")), safe_name(str(item.get("name", "")))
            path = self.dir / f"{digest}-{name}"
            if not digest or not path.is_file():
                raise Rejected(f"the uploaded file '{name}' is no longer on the server")
            renditions.append(Rendition("original", str(path), digest))
        return renditions

    # --- helpers -------------------------------------------------------------------------

    def _check_type(self, filename: str) -> None:
        if not safe_name(filename).lower().endswith(ALLOWED):
            raise IntakeRejected(
                "unsupported_type", f"File type not accepted. Allowed: {', '.join(ALLOWED)}."
            )

    def _check_url(self, url: str) -> None:
        parts = urlparse(url)
        if parts.scheme != "https" or not parts.hostname:
            raise IntakeRejected("bad_url", "Only https links are accepted.")
        addresses = self._resolver(parts.hostname)
        if not addresses or not all(_public(a) for a in addresses):
            raise IntakeRejected(
                "bad_url", "The link does not point to a public address on the internet."
            )


class CompositeMediaStore:
    """Routes each media item: uploaded through the API, or a Google Drive file from the Sheet."""

    def __init__(self, drive: MediaStore | None, uploads: MediaStore) -> None:
        self._drive = drive
        self._uploads = uploads

    def ensure_local(self, media: Sequence[Mapping[str, Any]]) -> list[Rendition]:
        found: list[Rendition | None] = [None] * len(media)
        for source, wanted in (
            (self._uploads, [i for i, m in enumerate(media) if m.get("media_id")]),
            (self._drive, [i for i, m in enumerate(media) if not m.get("media_id")]),
        ):
            if not wanted:
                continue
            if source is None:
                raise Rejected("this file needs Google Drive, which is not set up")
            for index, rendition in zip(
                wanted, source.ensure_local([media[i] for i in wanted]), strict=True
            ):
                found[index] = rendition
        return [r for r in found if r is not None]
