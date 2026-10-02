from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dk_publishing.adapters.platforms.instagram import InstagramPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.publishing import Rendition, VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.support import CAPS, T0
from tests.support.fake_graph import TOKEN
from tests.support.fake_instagram import FakeInstagram


class StaticToken:
    def token(self) -> str:
        return TOKEN


class TestInstagram(PublisherContract):
    # An empty caption is legal on Instagram; a story is not.
    bad_content: ClassVar[Mapping[str, Any]] = {"format": "story"}

    @pytest.fixture
    def publisher(self) -> Publisher:
        graph = GraphClient(
            version="v25.0", tokens=StaticToken(), transport=FakeInstagram().transport()
        )
        return InstagramPublisher(
            account_id="IG1", capabilities=CAPS, graph=graph, version="v25.0", sleep=lambda s: None
        )

    @pytest.fixture
    def media(self, tmp_path: Path) -> Sequence[Rendition]:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"REELBYTES")
        return [Rendition("original", str(path), "sha")]

    @pytest.fixture
    def snapshot(self) -> SnapshotFactory:
        variant_id = str(uuid.uuid4())

        def make(content: Mapping[str, Any]) -> VariantSnapshot:
            merged = {
                "format": "reel",
                "media": [{"name": "clip.mp4", "drive_file_id": "F", "md5": "m"}],
                **content,
            }
            return VariantSnapshot(variant_id, "dk", "instagram", "acct", T0, merged)

        return make
