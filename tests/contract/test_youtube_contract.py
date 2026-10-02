from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dk_publishing.adapters.platforms.youtube import YouTubePublisher
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.publishing import Rendition, VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.support import CAPS, T0
from tests.support.fake_youtube import FakeYouTube


class TestYouTube(PublisherContract):
    bad_content: ClassVar[Mapping[str, Any]] = {"title": ""}  # a video must have a title

    @pytest.fixture
    def publisher(self) -> Publisher:
        return YouTubePublisher(api=FakeYouTube(), capabilities=CAPS, sleep=lambda _: None)

    @pytest.fixture
    def media(self, tmp_path: Path) -> Sequence[Rendition]:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"VIDEOBYTES")
        return [Rendition("original", str(path), "sha")]

    @pytest.fixture
    def snapshot(self) -> SnapshotFactory:
        variant_id = str(uuid.uuid4())

        def make(content: Mapping[str, Any]) -> VariantSnapshot:
            merged = {
                "title": "Eid platter",
                "made_for_kids": "no",
                "media": [{"name": "clip.mp4", "drive_file_id": "F", "md5": "m"}],
                **content,
            }
            return VariantSnapshot(variant_id, "dk", "youtube", "acct", T0, merged)

        return make
