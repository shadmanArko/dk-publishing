"""The same contract, with the ledger in Postgres, which is what dry-run mode uses in production."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.application.ports import Publisher
from dk_publishing.domain.model import Variant
from dk_publishing.domain.publishing import VariantSnapshot
from tests.contract.publisher_contract import PublisherContract, SnapshotFactory
from tests.integration.conftest import TENANT, Seed
from tests.support import CAPS, T0


class TestDryRunPostgres(PublisherContract):
    @pytest.fixture
    def publisher(self, conninfo: str) -> Publisher:
        return DryRunPublisher("p", CAPS, PostgresLedger(conninfo))

    @pytest.fixture
    def snapshot(self, conninfo: str, seed: Seed) -> SnapshotFactory:
        post_id, account_id = seed.post_and_account()
        variant = Variant(str(uuid.uuid4()), TENANT, post_id, "p", account_id, T0)
        with PostgresUnitOfWork(conninfo) as uow:
            uow.variants.add(variant)  # the ledger's foreign key needs a real variant
            uow.commit()

        def make(content: Mapping[str, Any]) -> VariantSnapshot:
            return VariantSnapshot(variant.id, TENANT, "p", account_id, T0, content)

        return make
