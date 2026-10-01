"""The publish slice end to end against a real database and the dry-run publisher."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import psycopg
import pytest

from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher, PostgresLedger
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.application.ports import DueAction
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.dispatch import expire_variant, run_action, run_due
from dk_publishing.application.use_cases.housekeeping import flag_stale_publishing
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.application.use_cases.results import RunResult
from dk_publishing.domain import errors
from dk_publishing.domain.model import Actor, ActorKind, Variant
from dk_publishing.domain.planning import Action
from dk_publishing.domain.status import VariantStatus
from tests.integration.conftest import TENANT, Seed
from tests.support import CAPS, MIN, T0, FakeClock, H, ScriptedPublisher, SimulatedCrash

S = VariantStatus
R = RunResult
ME = Actor(ActorKind.HUMAN, "shadman")
CONTENT = {"caption": "Eid platter, hot from the pot"}
SLOT = T0 + 3 * H


class Rig:
    """A database, a clock and a scripted dry-run publisher wired into Services."""

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


@pytest.fixture
def rig(conninfo: str, seed: Seed) -> Rig:
    return Rig(conninfo, seed)


# approve -----------------------------------------------------------------------------------------


def test_approving_freezes_the_content_and_schedules_the_prepare(rig: Rig) -> None:
    variant = rig.approved()
    assert variant.status is S.APPROVED and variant.snapshot_hash
    status, action, at, *_ = rig.row(variant.id)
    assert (status, action, at) == ("approved", "prepare", SLOT - 30 * MIN)


def test_invalid_content_is_refused_with_a_reason_a_person_can_act_on(rig: Rig) -> None:
    variant = rig.draft()
    result, violations = approve(rig.services, variant.id, {"caption": ""}, ME)
    assert result is R.INVALID and violations[0].field == "caption"
    assert rig.get(variant.id).status is S.INVALID
    assert "Caption is empty" in rig.reasons(variant.id)[-1]


def test_approving_twice_is_a_no_op(rig: Rig) -> None:
    variant = rig.approved()
    assert approve(rig.services, variant.id, CONTENT, ME)[0] is R.SKIPPED
    assert rig.get(variant.id).version == variant.version


# the happy path ----------------------------------------------------------------------------------


def test_prepare_then_publish_goes_live_once(rig: Rig) -> None:
    variant = rig.prepared()
    assert rig.row(variant.id)[1:3] == ("publish", SLOT)
    rig.clock.set(SLOT + 20 * timedelta(seconds=1))

    assert publish_variant(rig.services, variant.id, variant.version) is R.PUBLISHED

    status, action, _, external_id, url, published_at = rig.row(variant.id)
    assert (status, action) == ("published", None)
    assert isinstance(external_id, str) and isinstance(url, str) and url.endswith(external_id)
    assert published_at == SLOT + timedelta(seconds=20)
    assert rig.posts(variant.id) == 1
    assert rig.attempts(variant.id) == [("prepare", "ok"), ("publish", "ok")]


def test_a_stale_run_key_does_nothing(rig: Rig) -> None:
    variant = rig.prepared()
    assert publish_variant(rig.services, variant.id, variant.version - 1) is R.SKIPPED
    assert rig.publisher.calls["publish"] == 0


# deadline and integrity --------------------------------------------------------------------------


def test_past_the_deadline_it_expires_instead_of_publishing(rig: Rig) -> None:
    variant = rig.prepared()
    rig.clock.set(SLOT + 2 * H + MIN)
    assert publish_variant(rig.services, variant.id, variant.version) is R.EXPIRED
    assert rig.get(variant.id).status is S.EXPIRED
    assert rig.posts(variant.id) == 0 and rig.publisher.calls["publish"] == 0


def test_an_unprepared_variant_past_its_deadline_expires_too(rig: Rig) -> None:
    variant = rig.approved()
    rig.clock.set(SLOT + 3 * H)
    assert prepare_variant(rig.services, variant.id, variant.version) is R.EXPIRED


def test_a_tampered_snapshot_is_never_sent(rig: Rig) -> None:
    variant = rig.prepared()
    with psycopg.connect(rig.conninfo) as conn:
        conn.execute(
            "UPDATE publishing.variants SET snapshot = %s::jsonb WHERE id = %s::uuid",
            ('{"caption": "something nobody approved"}', variant.id),
        )
    rig.clock.set(SLOT)
    with pytest.raises(errors.IntegrityError, match="no longer matches"):
        publish_variant(rig.services, variant.id, variant.version)
    assert rig.posts(variant.id) == 0 and rig.get(variant.id).status is S.PREPARED


# failures that are safe to retry -----------------------------------------------------------------


def test_a_retryable_failure_backs_off_then_succeeds(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed, publish=[errors.Retryable("connection reset")])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.RETRY_SCHEDULED
    again = rig.get(variant.id)
    assert again.status is S.PREPARED
    assert rig.row(variant.id)[1:3] == ("publish", SLOT + timedelta(seconds=30))
    assert rig.posts(variant.id) == 0

    rig.clock.advance(timedelta(seconds=30))
    assert publish_variant(rig.services, variant.id, again.version) is R.PUBLISHED
    assert rig.posts(variant.id) == 1
    assert rig.attempts(variant.id)[1:] == [("publish", "retryable"), ("publish", "ok")]


def test_a_rate_limit_waits_exactly_as_long_as_the_platform_asked(
    conninfo: str, seed: Seed
) -> None:
    rig = Rig(conninfo, seed, publish=[errors.RateLimited(timedelta(minutes=17))])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.RETRY_SCHEDULED
    assert rig.row(variant.id)[2] == SLOT + 17 * MIN


def test_it_gives_up_after_the_backoff_steps_are_used(conninfo: str, seed: Seed) -> None:
    flaky = [errors.Retryable("down")] * 4
    rig = Rig(conninfo, seed, publish=flaky)
    variant = rig.prepared()
    rig.clock.set(SLOT)
    results = []
    for _ in range(4):
        current = rig.get(variant.id)
        results.append(publish_variant(rig.services, variant.id, current.version))
        rig.clock.advance(10 * MIN)
    assert results == [R.RETRY_SCHEDULED] * 3 + [R.FAILED]
    assert rig.get(variant.id).status is S.FAILED
    assert "gave up after 4" in rig.reasons(variant.id)[-1]
    assert rig.posts(variant.id) == 0


def test_a_retry_that_would_land_past_the_deadline_fails_instead(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed, publish=[errors.RateLimited(3 * H)])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.FAILED


def test_a_failed_prepare_retries_from_approved(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed, prepare=[errors.Retryable("upload stalled")])
    variant = rig.approved()
    rig.clock.set(SLOT - 30 * MIN)
    assert prepare_variant(rig.services, variant.id, variant.version) is R.RETRY_SCHEDULED
    assert rig.get(variant.id).status is S.APPROVED
    assert rig.row(variant.id)[1:3] == ("prepare", SLOT - 30 * MIN + timedelta(seconds=30))


def test_an_uncertain_prepare_is_safe_to_repeat(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed, prepare=[errors.UnknownOutcome("timeout")])
    variant = rig.approved()
    assert prepare_variant(rig.services, variant.id, variant.version) is R.RETRY_SCHEDULED
    assert rig.get(variant.id).status is S.APPROVED


# failures that are not --------------------------------------------------------------------------


def test_a_rejection_fails_the_variant_with_the_platforms_message(
    conninfo: str, seed: Seed
) -> None:
    rig = Rig(conninfo, seed, publish=[errors.Rejected("caption violates policy 4.2")])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.FAILED
    assert "caption violates policy 4.2" in rig.reasons(variant.id)[-1]


def test_a_dead_token_parks_the_variant_for_a_person(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed, publish=[errors.AuthFailed("token revoked")])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.PARKED
    assert rig.row(variant.id)[:3] == ("prepared", None, None)  # nothing scheduled
    assert rig.attempts(variant.id)[-1] == ("publish", "auth_failed")


# the never-post-twice guarantee ------------------------------------------------------------------


def test_uncertain_and_the_post_never_went_out_republishes_exactly_once(
    conninfo: str, seed: Seed
) -> None:
    rig = Rig(conninfo, seed, publish=[errors.UnknownOutcome("timed out")])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.FLAGGED_UNKNOWN
    unknown = rig.get(variant.id)
    assert (unknown.status, rig.row(variant.id)[1]) == (S.UNKNOWN, "reconcile")

    assert reconcile_variant(rig.services, variant.id, unknown.version) is R.NOT_LIVE
    prepared = rig.get(variant.id)
    assert prepared.status is S.PREPARED
    assert publish_variant(rig.services, variant.id, prepared.version) is R.PUBLISHED
    assert rig.posts(variant.id) == 1


def test_uncertain_but_the_post_did_go_out_is_never_published_again(
    conninfo: str, seed: Seed
) -> None:
    lost_response = ("after", errors.UnknownOutcome("response lost"))
    rig = Rig(conninfo, seed, publish=[lost_response])
    variant = rig.prepared()
    rig.clock.set(SLOT)
    assert publish_variant(rig.services, variant.id, variant.version) is R.FLAGGED_UNKNOWN
    assert rig.posts(variant.id) == 1  # the platform did post

    unknown = rig.get(variant.id)
    assert reconcile_variant(rig.services, variant.id, unknown.version) is R.PUBLISHED
    assert rig.publisher.calls["publish"] == 1  # never called again
    assert rig.posts(variant.id) == 1
    assert rig.row(variant.id)[3] is not None  # the real id was recovered


def test_a_platform_that_cannot_answer_fails_the_variant_for_a_human(
    conninfo: str, seed: Seed
) -> None:
    rig = Rig(
        conninfo,
        seed,
        publish=[errors.UnknownOutcome("timed out")],
        find_live=[errors.Retryable("status endpoint down")],
    )
    variant = rig.prepared()
    rig.clock.set(SLOT)
    publish_variant(rig.services, variant.id, variant.version)
    unknown = rig.get(variant.id)
    assert reconcile_variant(rig.services, variant.id, unknown.version) is R.FAILED
    assert "check by hand" in rig.reasons(variant.id)[-1]
    assert rig.publisher.calls["publish"] == 1


def test_chaos_a_crash_right_after_the_request_is_sent_still_yields_one_post(
    conninfo: str, seed: Seed
) -> None:
    """The plan's duplicate test: kill the run once the publish request is out."""
    rig = Rig(conninfo, seed, publish=[("after", SimulatedCrash())])
    variant = rig.prepared()
    rig.clock.set(SLOT)

    with pytest.raises(SimulatedCrash):
        publish_variant(rig.services, variant.id, variant.version)
    assert rig.get(variant.id).status is S.PUBLISHING  # the run died; nothing recorded the outcome
    assert rig.posts(variant.id) == 1

    # Fresh enough: housekeeping leaves it alone.
    rig.clock.advance(5 * MIN)
    assert flag_stale_publishing(rig.services) == []

    rig.clock.advance(6 * MIN)
    assert flag_stale_publishing(rig.services) == [variant.id]
    unknown = rig.get(variant.id)
    assert unknown.status is S.UNKNOWN
    assert rig.attempts(variant.id)[-1] == ("publish", "unknown")  # the dangling intent is closed

    assert reconcile_variant(rig.services, variant.id, unknown.version) is R.PUBLISHED
    assert rig.posts(variant.id) == 1
    assert rig.publisher.calls["publish"] == 1


def test_two_runs_racing_to_publish_make_one_post(conninfo: str, seed: Seed) -> None:
    rig = Rig(conninfo, seed)
    variant = rig.prepared()
    rig.clock.set(SLOT)
    barrier = threading.Barrier(6)
    results: list[RunResult] = []

    def run() -> None:
        barrier.wait()
        results.append(publish_variant(rig.services, variant.id, variant.version))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == sorted([R.PUBLISHED] + [R.LOST_RACE] * 5)
    assert rig.posts(variant.id) == 1 and rig.publisher.calls["publish"] == 1


# the scheduler tick ------------------------------------------------------------------------------


def test_expire_variant_only_touches_unpublished_variants(rig: Rig) -> None:
    variant = rig.prepared()
    live = rig.approved()  # a second, separate variant
    assert expire_variant(rig.services, variant.id, variant.version - 1) is R.SKIPPED
    rig.clock.set(SLOT + 3 * H)
    assert expire_variant(rig.services, variant.id, variant.version) is R.EXPIRED
    assert expire_variant(rig.services, variant.id, variant.version + 1) is R.SKIPPED
    assert rig.get(live.id).status is S.APPROVED


def test_unbuilt_actions_say_so(rig: Rig) -> None:
    item = DueAction("v", Action.FETCH_MEDIA, T0, 1)
    with pytest.raises(NotImplementedError):
        run_action(rig.services, item)


def test_a_week_of_posts_is_published_on_time_exactly_once_each(conninfo: str, seed: Seed) -> None:
    """Rehearsal: a day of slots, a scheduler ticking every 30 s, a flaky platform, one crash."""
    flaky = [None, errors.Retryable("blip"), None, errors.RateLimited(2 * MIN), None]
    rig = Rig(conninfo, seed, publish=flaky)
    slots = [T0 + timedelta(minutes=m) for m in (45, 61, 90, 90, 150, 400)]
    ids = []
    for slot in slots:
        variant = rig.draft(publish_at=slot)
        assert approve(rig.services, variant.id, CONTENT, ME)[0] is R.APPROVED
        ids.append(variant.id)

    end = T0 + 10 * H
    while rig.clock.now() < end:
        run_due(rig.services)
        rig.clock.advance(timedelta(seconds=30))

    for variant_id, slot in zip(ids, slots, strict=True):
        status, _, _, _, _, published_at = rig.row(variant_id)
        assert status == "published", (variant_id, status)
        assert rig.posts(variant_id) == 1
        late = published_at - slot  # type: ignore[operator]
        assert timedelta(0) <= late <= timedelta(minutes=4), late
    assert len({r for r in ids}) == len(slots)


def test_unknown_platform_is_loud(rig: Rig) -> None:
    variant = replace(rig.draft(), platform="nope")
    with psycopg.connect(rig.conninfo) as conn:
        conn.execute(
            "UPDATE publishing.variants SET platform = 'nope' WHERE id = %s::uuid", (variant.id,)
        )
    from dk_publishing.adapters.platforms.registry import UnknownPlatform

    with pytest.raises(UnknownPlatform):
        approve(rig.services, variant.id, CONTENT, ME)
