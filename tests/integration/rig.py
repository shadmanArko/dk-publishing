"""A database, a clock and a scripted dry-run publisher wired into Services."""

from __future__ import annotations

import uuid
from datetime import datetime

import psycopg

from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Actor, ActorKind, Variant
from dk_publishing.domain.status import VariantStatus
from tests.integration.conftest import TENANT, Seed
from tests.support import CAPS, T0, FakeClock, H, ScriptedPublisher

S = VariantStatus
R = RunResult
ME = Actor(ActorKind.HUMAN, "shadman")
CONTENT = {"caption": "Eid platter, hot from the pot"}
SLOT = T0 + 3 * H


class Rig:
    def __init__(self, conninfo: str, seed: Seed, **script: object) -> None:
        self.conninfo = conninfo
        self.seed = seed
        self.clock = FakeClock()
        self.ledger = PostgresLedger(conninfo)
        self.publisher = ScriptedPublisher(
            DryRunPublisher("p", CAPS, self.ledger),
            **script,  # type: ignore[arg-type]
        )
        self.services = Services(
            uow=lambda: PostgresUnitOfWork(conninfo),
            publishers=StaticPublisherRegistry({"p": self.publisher}),
            clock=self.clock,
        )
        self._keys = 0

    def draft(self, publish_at: datetime = SLOT) -> Variant:
        self._keys += 1
        post_id, account_id = self.seed.post_and_account(f"DK-{self._keys}")
        variant = Variant(
            id=str(uuid.uuid4()),
            tenant_id=TENANT,
            post_id=post_id,
            platform="p",
            account_id=account_id,
            publish_at=publish_at,
        )
        with self.services.uow() as uow:
            uow.variants.add(variant)
            uow.commit()
        return variant

    def approved(self) -> Variant:
        variant = self.draft()
        assert approve(self.services, variant.id, CONTENT, ME)[0] is R.APPROVED
        return self.get(variant.id)

    def prepared(self) -> Variant:
        variant = self.approved()
        assert prepare_variant(self.services, variant.id, variant.version) is R.PREPARED
        return self.get(variant.id)

    def get(self, variant_id: str) -> Variant:
        with self.services.uow() as uow:
            found = uow.variants.get(variant_id)
        assert found is not None
        return found

    def row(self, variant_id: str) -> tuple[object, ...]:
        with psycopg.connect(self.conninfo) as conn:
            row = conn.execute(
                """SELECT status, next_action, next_action_at, external_id, external_url,
                          published_at FROM publishing.variants WHERE id = %s::uuid""",
                (variant_id,),
            ).fetchone()
        assert row is not None
        return row

    def attempts(self, variant_id: str) -> list[tuple[str, str | None]]:
        with psycopg.connect(self.conninfo) as conn:
            return [
                (r[0], r[1])
                for r in conn.execute(
                    """SELECT phase, outcome FROM publishing.publish_attempts
                       WHERE variant_id = %s::uuid ORDER BY started_at, id""",
                    (variant_id,),
                ).fetchall()
            ]

    def posts(self, variant_id: str) -> int:
        return len(self.ledger.posts_for(TENANT, variant_id))

    def reasons(self, variant_id: str) -> list[str]:
        with self.services.uow() as uow:
            return [e.reason for e in uow.variants.events(variant_id)]
