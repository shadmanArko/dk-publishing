from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, InMemoryLedger
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.publishing import VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.support import CAPS, T0


class TestDryRunInMemory(PublisherContract):
    @pytest.fixture
    def publisher(self) -> Publisher:
        return DryRunPublisher("p", CAPS, InMemoryLedger())

    @pytest.fixture
    def snapshot(self) -> SnapshotFactory:
        variant_id = str(uuid.uuid4())

        def make(content: Mapping[str, Any]) -> VariantSnapshot:
            return VariantSnapshot(variant_id, "dk", "p", str(uuid.uuid4()), T0, content)

        return make


def test_a_caption_over_the_limit_is_explained() -> None:
    publisher = DryRunPublisher("p", CAPS, InMemoryLedger(), max_caption=10)
    snap = VariantSnapshot("v", "dk", "p", "a", T0, {"caption": "x" * 2315})
    [problem] = publisher.validate(snap)
    assert problem.message == "Caption is 2,315 characters; this platform allows 10."


def test_publishing_twice_makes_two_posts_so_a_duplicate_cannot_hide() -> None:
    ledger = InMemoryLedger()
    publisher = DryRunPublisher("p", CAPS, ledger)
    snap = VariantSnapshot("v", "dk", "p", "a", T0, {"caption": "hi"})
    handle = publisher.prepare(snap, ())
    publisher.publish(handle)
    publisher.publish(handle)
    assert len(ledger.posts_for("dk", "v")) == 2
