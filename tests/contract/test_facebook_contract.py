from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.publishing import VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.support import CAPS, T0
from tests.support.fake_graph import TOKEN, FakeGraph


class StaticToken:
    def token(self) -> str:
        return TOKEN


class TestFacebook(PublisherContract):
    @pytest.fixture
    def publisher(self) -> Publisher:
        graph = GraphClient(
            version="v25.0", tokens=StaticToken(), transport=FakeGraph().transport()
        )
        return FacebookPublisher(page_id="PAGE", capabilities=CAPS, graph=graph)

    @pytest.fixture
    def snapshot(self) -> SnapshotFactory:
        variant_id = str(uuid.uuid4())

        def make(content: Mapping[str, Any]) -> VariantSnapshot:
            # The contract's generic content has no format; a text post is the simplest valid one.
            return VariantSnapshot(
                variant_id, "dk", "facebook", "acct", T0, {"format": "post", **content}
            )

        return make
