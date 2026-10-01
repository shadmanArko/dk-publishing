"""The suite every platform adapter must pass.

Subclass `PublisherContract` and provide `publisher` and `snapshot`. A new adapter is not done
until its subclass is green, which is what keeps all eight substitutable for one another.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from dk_publishing.application.ports import Publisher
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import PublishingError
from dk_publishing.domain.publishing import LivePost, VariantSnapshot, Violation

SnapshotFactory = Callable[[Mapping[str, Any]], VariantSnapshot]
GOOD = {"caption": "Kacchi biryani, Friday only"}


class PublisherContract:
    @pytest.fixture
    def publisher(self) -> Publisher:
        raise NotImplementedError

    @pytest.fixture
    def snapshot(self) -> SnapshotFactory:
        raise NotImplementedError

    def test_declares_valid_capabilities(self, publisher: Publisher) -> None:
        assert isinstance(publisher.capabilities, Capabilities)

    def test_accepts_good_content(self, publisher: Publisher, snapshot: SnapshotFactory) -> None:
        assert publisher.validate(snapshot(GOOD)) == []

    def test_reports_problems_in_plain_words(
        self, publisher: Publisher, snapshot: SnapshotFactory
    ) -> None:
        problems = publisher.validate(snapshot({"caption": ""}))
        assert problems and all(isinstance(p, Violation) for p in problems)
        assert all(p.field and p.message for p in problems)

    def test_prepare_returns_json_the_database_can_store(
        self, publisher: Publisher, snapshot: SnapshotFactory
    ) -> None:
        handle = publisher.prepare(snapshot(GOOD), ())
        assert json.loads(json.dumps(handle)) == handle

    def test_prepare_posts_nothing_and_can_be_repeated(
        self, publisher: Publisher, snapshot: SnapshotFactory
    ) -> None:
        snap = snapshot(GOOD)
        first, second = publisher.prepare(snap, ()), publisher.prepare(snap, ())
        assert first == second
        assert publisher.find_live(snap, first) is None

    def test_publish_goes_live_and_can_then_be_found(
        self, publisher: Publisher, snapshot: SnapshotFactory
    ) -> None:
        snap = snapshot(GOOD)
        handle = publisher.prepare(snap, ())
        assert publisher.find_live(snap, handle) is None  # confirmed not live, before publishing
        live = publisher.publish(handle)
        assert isinstance(live, LivePost) and live.external_id
        found = publisher.find_live(snap, handle)
        assert found is not None and found.external_id == live.external_id

    def test_a_handle_it_cannot_use_raises_a_domain_error(self, publisher: Publisher) -> None:
        with pytest.raises(PublishingError):
            publisher.publish({"unrelated": True})
