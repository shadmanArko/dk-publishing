"""Showing a draft's media with the existing tokenised public links, and reusing them (ADR 0025)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tests.support import FakeClock

from dk_publishing.adapters.media.preview import KEEP, PublicLinkPreview
from dk_publishing.adapters.media.public import PublicMediaStore
from dk_publishing.adapters.media.uploads import UploadStore

BASE = "https://media.example.test"


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        uploads = UploadStore(tmp_path / "media")
        writer = uploads.begin("reel.mp4")
        writer.write(b"video-bytes")
        stored = writer.finish()
        self.public_dir = tmp_path / "public"
        self.clock = FakeClock()
        self.preview = PublicLinkPreview(
            uploads, PublicMediaStore(self.public_dir, BASE), self.clock.now
        )
        self.item: dict[str, Any] = {
            "name": stored.filename,
            "media_id": "m1",
            "sha256": stored.sha256,
            "size": stored.size,
        }

    def folders(self) -> list[Path]:
        return sorted(self.public_dir.iterdir()) if self.public_dir.exists() else []


def test_an_uploaded_file_gets_the_same_unguessable_public_link_platforms_use(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    url = rig.preview.url_for(rig.item)
    assert url is not None and url.startswith(BASE + "/") and url.endswith("-reel.mp4")
    token = urlparse(url).path.split("/")[1]
    assert len(token) >= 20  # a random token folder, never a path into the media disk
    assert (rig.public_dir / token / url.rsplit("/", 1)[1]).read_bytes() == b"video-bytes"


def test_a_reload_reuses_the_link_instead_of_leaving_a_folder_per_view(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    first = rig.preview.url_for(rig.item)
    for _ in range(5):
        assert rig.preview.url_for(rig.item) == first
    assert len(rig.folders()) == 1


def test_a_link_is_renewed_before_the_24_hour_sweep_could_remove_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    first = rig.preview.url_for(rig.item)
    rig.clock.advance(KEEP + timedelta(minutes=1))
    second = rig.preview.url_for(rig.item)
    assert second is not None and second != first and timedelta(hours=24) > KEEP


def test_a_removed_file_and_a_non_upload_have_no_preview(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.preview.url_for({**rig.item, "media_id": ""}) is None  # a Drive file: not ours
    assert rig.preview.url_for({**rig.item, "sha256": "0" * 64, "name": "gone.mp4"}) is None
    assert rig.folders() == []
