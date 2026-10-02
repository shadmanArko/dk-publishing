from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dk_publishing.adapters.platforms.tiktok import build_tiktok
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.publishing import Rendition, VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.support import CAPS, T0
from tests.support.fake_telegram import FakeNotifier


class MemoryLog:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def was_sent(self, key: str) -> bool:
        return key in self.keys

    def mark_sent(self, key: str) -> None:
        self.keys.add(key)


class TestTikTok(PublisherContract):
    bad_content: ClassVar[Mapping[str, Any]] = {"privacy_level": ""}  # TikTok has no default

    @pytest.fixture
    def publisher(self) -> Publisher:
        return build_tiktok(CAPS, FakeNotifier(), MemoryLog())

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
                "caption": "Kacchi biryani, Friday only",
                "privacy_level": "public",
                "media": [{"name": "clip.mp4", "drive_file_id": "F", "md5": "m"}],
                **content,
            }
            return VariantSnapshot(variant_id, "dk", "tiktok", "acct", T0, merged)

        return make
