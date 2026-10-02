from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.threads import HOST, ThreadsPublisher
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.publishing import VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.support import CAPS, T0
from tests.support.fake_threads import THREADS_TOKEN, FakeThreads


class StaticToken:
    def token(self) -> str:
        return THREADS_TOKEN


class TestThreads(PublisherContract):
    @pytest.fixture
    def publisher(self) -> Publisher:
        graph = GraphClient(
            version="v1.0",
            tokens=StaticToken(),
            transport=FakeThreads().transport(),
            host=HOST,
            video_host=HOST,
        )
        return ThreadsPublisher(user_id="TH1", capabilities=CAPS, graph=graph, sleep=lambda s: None)

    @pytest.fixture
    def snapshot(self) -> SnapshotFactory:
        variant_id = str(uuid.uuid4())

        def make(content: Mapping[str, Any]) -> VariantSnapshot:
            return VariantSnapshot(
                variant_id, "dk", "threads", "acct", T0, {"format": "text", **content}
            )

        return make
