"""A TikTok-style hand-over through the real use cases and database, then into the Sheet text."""

from __future__ import annotations

import psycopg

from dk_publishing.adapters.persistence.sent_log import PostgresSentLog
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.adapters.platforms.tiktok import build_tiktok
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.alerts import send_alerts
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain.model import Variant
from dk_publishing.domain.publishing import ASSISTED_PREFIX
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.sync import SENT_TO_YOU, row_status
from tests.integration.conftest import TENANT, Seed
from tests.integration.rig import ME, SLOT
from tests.support import CAPS, FakeClock
from tests.support.fake_telegram import FakeNotifier

R = RunResult
S = VariantStatus
CONTENT = {"caption": "Kacchi tonight", "privacy_level": "public", "media": [{"name": "clip.mp4"}]}


class Files:
    """Stands in for the Drive download: hands every post the same local video."""

    def __init__(self, path: str) -> None:
        self._path = path

    def ensure_local(self, media: object) -> list[object]:
        from dk_publishing.domain.publishing import Rendition

        return [Rendition("original", self._path, "sha")]


def services(conninfo: str, telegram: FakeNotifier, clock: FakeClock, clip: str) -> Services:
    return Services(
        uow=lambda: PostgresUnitOfWork(conninfo),
        publishers=StaticPublisherRegistry(
            {"p": build_tiktok(CAPS, telegram, PostgresSentLog(conninfo))}
        ),
        clock=clock,
        media_store=Files(clip),  # type: ignore[arg-type]
    )


def test_a_post_is_handed_over_once_and_the_sheet_says_sent_to_you(
    conninfo: str, seed: Seed, tmp_path: object
) -> None:
    from pathlib import Path

    clip = Path(str(tmp_path)) / "clip.mp4"
    clip.write_bytes(b"VIDEO")
    telegram, clock = FakeNotifier(), FakeClock()
    svc = services(conninfo, telegram, clock, str(clip))

    post_id, account_id = seed.post_and_account("DK-TT-1")
    variant = Variant(
        id="00000000-0000-0000-0000-0000000000a1",
        tenant_id=TENANT,
        post_id=post_id,
        platform="p",
        account_id=account_id,
        publish_at=SLOT,
    )
    with svc.uow() as uow:
        uow.variants.add(variant)
        uow.commit()
    assert approve(svc, variant.id, CONTENT, ME)[0] is R.APPROVED
    with svc.uow() as uow:
        current = uow.variants.get(variant.id)
    assert current is not None and prepare_variant(svc, variant.id, current.version) is R.PREPARED
    assert telegram.sent == []  # preparing sends nothing

    clock.set(SLOT)
    with svc.uow() as uow:
        prepared = uow.variants.get(variant.id)
    assert prepared is not None
    assert publish_variant(svc, variant.id, prepared.version) is R.PUBLISHED
    assert len(telegram.sent) == 1 and "Post this on TikTok now" in telegram.sent[0]
    assert len(telegram.videos) == 1

    with psycopg.connect(conninfo) as conn:
        status, external_id, url = conn.execute(
            "SELECT status, external_id, external_url FROM publishing.variants WHERE id = %s::uuid",
            (variant.id,),
        ).fetchone() or ("", "", "")
    assert status == "published" and external_id.startswith(ASSISTED_PREFIX) and url is None

    text = row_status(
        enabled=True,
        variant_status=S.PUBLISHED,
        problems=[],
        external_url=url,
        external_id=external_id,
        last_reason=None,
        edited_while_live=False,
    )
    assert text[0] == SENT_TO_YOU

    # Nothing can send it a second time, and an alert is not raised for a normal hand-over.
    assert reconcile_variant(svc, variant.id, prepared.version) is R.SKIPPED
    assert send_alerts(svc, telegram).sent == 0
