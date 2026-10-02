"""Alerts and the digest against a real database."""

from __future__ import annotations

from datetime import timedelta

import psycopg

from dk_publishing.application.use_cases.alerts import send_alerts, send_digest, send_run_failure
from dk_publishing.application.use_cases.dispatch import expire_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.domain import errors
from tests.integration.conftest import Seed
from tests.integration.rig import SLOT, R, Rig
from tests.support import H
from tests.support.fake_telegram import FakeNotifier


def failed_rig(
    conninfo: str, seed: Seed, message: str = "caption violates policy 4.2"
) -> tuple[Rig, str]:
    rig = Rig(conninfo, seed, publish=[errors.Rejected(message)])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.FAILED
    return rig, variant.id


def test_a_failed_post_is_reported_once_with_the_reason(conninfo: str, seed: Seed) -> None:
    rig, _ = failed_rig(conninfo, seed)
    telegram = FakeNotifier()
    assert send_alerts(rig.services, telegram).sent == 1
    [text] = telegram.sent
    assert "Failed" in text and "caption violates policy 4.2" in text and "14.11. 16:00" in text

    assert send_alerts(rig.services, telegram).sent == 0  # already told
    assert len(telegram.sent) == 1


def test_a_telegram_outage_loses_nothing_and_the_alert_goes_out_when_it_is_back(
    conninfo: str, seed: Seed
) -> None:
    rig, _ = failed_rig(conninfo, seed)
    telegram = FakeNotifier()
    telegram.down = True
    report = send_alerts(rig.services, telegram)
    assert report.sent == 0 and "could not reach Telegram" in report.failed[0]

    telegram.down = False
    assert send_alerts(rig.services, telegram).sent == 1
    assert send_alerts(rig.services, telegram).sent == 0


def test_an_expired_post_is_reported(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    variant = rig.approved()
    rig.clock.set(SLOT + 3 * H)  # past the 2 hour deadline
    assert expire_variant(rig.services, variant.id, variant.version) is R.EXPIRED
    telegram = FakeNotifier()
    send_alerts(rig.services, telegram)
    assert "Missed its slot" in telegram.sent[0]


def test_ordinary_progress_is_not_an_alert(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    rig.prepared()
    telegram = FakeNotifier()
    assert send_alerts(rig.services, telegram).sent == 0 and telegram.sent == []


def test_old_history_is_not_replayed_on_the_first_start(conninfo: str, seed: Seed) -> None:
    rig, _ = failed_rig(conninfo, seed)
    rig.clock.advance(timedelta(days=3))
    telegram = FakeNotifier()
    assert send_alerts(rig.services, telegram).sent == 0


def test_text_from_a_post_cannot_inject_markup(conninfo: str, seed: Seed) -> None:
    rig, _ = failed_rig(conninfo, seed, "bad <b>tag</b> & more")
    telegram = FakeNotifier()
    send_alerts(rig.services, telegram)
    assert "bad &lt;b&gt;tag&lt;/b&gt; &amp; more" in telegram.sent[0]


def test_a_failed_job_is_reported_once_per_hour(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    telegram = FakeNotifier()
    assert send_run_failure(
        rig.services, telegram, run_id="abcdef123", job="sync_sheet", error="halted"
    )
    assert not send_run_failure(
        rig.services, telegram, run_id="zzz", job="sync_sheet", error="halted"
    )
    assert send_run_failure(rig.services, telegram, run_id="q", job="housekeeping", error="boom")
    rig.clock.advance(timedelta(hours=1))
    assert send_run_failure(rig.services, telegram, run_id="n", job="sync_sheet", error="halted")
    assert len(telegram.sent) == 3 and "halted" in telegram.sent[0]


def test_the_digest_lists_today_yesterday_and_warnings_and_goes_out_once(
    conninfo: str, seed: Seed
) -> None:
    rig = Rig(conninfo, seed)
    rig.approved()  # slot at 15:00 Berlin the same day (T0 + 3h = 15:00 UTC = 16:00 Berlin)
    rig.clock.set(SLOT.replace(hour=6, minute=0))  # 07:00 Berlin, the morning of the slot
    telegram = FakeNotifier()
    assert send_digest(rig.services, telegram, ["The Threads token expires in 3 days"])
    [text] = telegram.sent
    assert "16:00" in text and "Needs you" in text and "expires in 3 days" in text
    assert "Yesterday</b>: 0 published" in text

    assert not send_digest(rig.services, telegram)
    assert len(telegram.sent) == 1


def test_an_empty_day_still_gets_a_digest(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    telegram = FakeNotifier()
    send_digest(rig.services, telegram)
    assert "Nothing scheduled today." in telegram.sent[0]


def test_yesterdays_published_post_shows_its_lateness(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    variant = rig.prepared()
    rig.clock.set(SLOT + timedelta(seconds=42))
    assert publish_variant(rig.services, variant.id, variant.version) is R.PUBLISHED
    rig.clock.advance(timedelta(days=1))
    telegram = FakeNotifier()
    send_digest(rig.services, telegram)
    assert "1 published, 0 not" in telegram.sent[0] and "42s after the slot" in telegram.sent[0]


def test_the_sent_record_is_per_tenant_key(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    with rig.services.uow() as uow:
        assert uow.alerts.mark_sent("dk", "k", rig.clock.now())
        assert not uow.alerts.mark_sent("dk", "k", rig.clock.now())
        assert uow.alerts.mark_sent("other", "k", rig.clock.now())
        uow.commit()
    with psycopg.connect(conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM publishing.alerts_sent").fetchone() == (2,)
