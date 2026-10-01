"""Native delivery: the platform holds the post and publishes it by itself at the slot."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.dispatch import run_action, run_due
from dk_publishing.application.use_cases.housekeeping import fail_stale_scheduling
from dk_publishing.application.use_cases.native import withdraw_native
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.application.use_cases.schedule_native import schedule_native_variant
from dk_publishing.domain import errors
from dk_publishing.domain.errors import IntegrityError
from dk_publishing.domain.model import Variant
from dk_publishing.domain.planning import Action
from tests.integration.conftest import TENANT, Seed
from tests.integration.rig import ME, SLOT, R, Rig, S
from tests.support import MIN, NATIVE_CAPS, T0, SimulatedCrash

CAPTION = "Kacchi biryani for Eid"


def rig_with(conninfo: str, seed: Seed, **script: object) -> Rig:
    return Rig(conninfo, seed, caps=NATIVE_CAPS, **script)


def approved(rig: Rig, delivery: str | None = "native", **draft: object) -> Variant:
    variant = rig.draft(**draft)  # type: ignore[arg-type]
    content: dict[str, object] = {"caption": CAPTION}
    if delivery:
        content["delivery"] = delivery
    assert approve(rig.services, variant.id, content, ME)[0] is R.APPROVED
    return rig.get(variant.id)


def scheduled(rig: Rig) -> Variant:
    v = approved(rig)
    assert schedule_native_variant(rig.services, v.id, v.version) is R.SCHEDULED
    return rig.get(v.id)


def held_ids(rig: Rig, variant_id: str) -> list[str]:
    return [p.external_id for p in rig.ledger.posts_for(TENANT, variant_id)]


# --- the happy path ---------------------------------------------------------------------------


def test_a_native_post_is_handed_over_and_the_platform_publishes_it(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    v = approved(rig)
    assert rig.row(v.id)[1] == "schedule_native"  # due now: the platform's window is open

    assert schedule_native_variant(rig.services, v.id, v.version) is R.SCHEDULED
    held = rig.get(v.id)
    assert held.status is S.SCHEDULED_NATIVE
    assert rig.row(v.id)[1:3] == ("reconcile", SLOT + 10 * MIN)  # verify after the slot
    assert (
        len(held_ids(rig, v.id)) == 1 and rig.ledger.live_for(TENANT, v.id, rig.clock.now()) == []
    )
    assert rig.attempts(v.id) == [("schedule_native", "ok")]

    rig.clock.set(SLOT + 10 * MIN)  # this system did nothing in between; the platform published
    assert reconcile_variant(rig.services, v.id, held.version) is R.PUBLISHED
    status, _, _, external_id, url, _ = rig.row(v.id)
    assert (status, external_id) == ("published", held_ids(rig, v.id)[0]) and url
    assert rig.publisher.calls == {
        "prepare": 0,
        "publish": 0,
        "find_live": 1,
        "schedule": 1,
        "cancel": 0,
    }


def test_a_row_without_a_delivery_stays_direct_even_where_native_is_possible(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    v = approved(rig, delivery=None)
    assert rig.row(v.id)[1:3] == ("prepare", SLOT - 5 * MIN)
    v2 = approved(rig, delivery="direct")
    assert rig.row(v2.id)[1] == "prepare"


def test_a_native_post_the_platform_never_publishes_is_failed_for_a_person(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    v = scheduled(rig)
    rig.ledger.cancel(TENANT, held_ids(rig, v.id)[0], rig.clock.now())  # dropped on the platform
    rig.clock.set(SLOT + 10 * MIN)
    assert reconcile_variant(rig.services, v.id, v.version) is R.FAILED
    assert "did not publish the post it was holding" in rig.reasons(v.id)[-1]


def test_a_platform_that_cannot_confirm_a_held_post_fails_it_for_a_person(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed, find_live=[errors.Retryable("status endpoint down")])
    v = scheduled(rig)
    rig.clock.set(SLOT + 10 * MIN)
    assert reconcile_variant(rig.services, v.id, v.version) is R.FAILED
    assert "check by hand" in rig.reasons(v.id)[-1]


def test_too_late_to_schedule_the_post_is_published_directly_instead(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    slot = T0 + 15 * MIN
    v = approved(rig, publish_at=slot)
    rig.clock.set(T0 + 9 * MIN)  # the scheduling run was delayed: only 6 minutes remain, 10 needed

    assert schedule_native_variant(rig.services, v.id, v.version) is R.RETRY_SCHEDULED
    back = rig.get(v.id)
    assert back.status is S.APPROVED and rig.row(v.id)[1] == "prepare"
    assert "publishing directly" in rig.reasons(v.id)[-1] and rig.publisher.calls["schedule"] == 0

    assert prepare_variant(rig.services, v.id, back.version) is R.PREPARED
    rig.clock.set(slot)
    assert publish_variant(rig.services, v.id, rig.get(v.id).version) is R.PUBLISHED
    assert len(held_ids(rig, v.id)) == 1 and rig.publisher.calls["publish"] == 1


def test_a_post_that_missed_its_deadline_expires_instead_of_being_scheduled(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    v = approved(rig)
    rig.clock.set(SLOT + 3 * timedelta(hours=1))
    assert schedule_native_variant(rig.services, v.id, v.version) is R.EXPIRED
    assert rig.publisher.calls["schedule"] == 0


def test_a_stale_run_key_schedules_nothing(conninfo: str, seed: Seed) -> None:
    rig = rig_with(conninfo, seed)
    v = approved(rig)
    assert schedule_native_variant(rig.services, v.id, v.version - 1) is R.SKIPPED
    assert rig.publisher.calls["schedule"] == 0


# --- failures while handing the post over -----------------------------------------------------


def test_a_blip_while_scheduling_retries_and_schedules_once(conninfo: str, seed: Seed) -> None:
    rig = rig_with(conninfo, seed, schedule=[errors.Retryable("connection reset")])
    v = approved(rig)
    assert schedule_native_variant(rig.services, v.id, v.version) is R.RETRY_SCHEDULED
    again = rig.get(v.id)
    assert again.status is S.APPROVED and rig.row(v.id)[1:3] == (
        "schedule_native",
        T0 + timedelta(seconds=30),
    )

    rig.clock.advance(timedelta(seconds=30))
    assert schedule_native_variant(rig.services, v.id, again.version) is R.SCHEDULED
    assert len(held_ids(rig, v.id)) == 1 and rig.publisher.calls["schedule"] == 2


def test_a_dead_token_parks_the_post_and_a_rejection_fails_it(conninfo: str, seed: Seed) -> None:
    rig = rig_with(
        conninfo, seed, schedule=[errors.AuthFailed("expired"), errors.Rejected("not allowed")]
    )
    parked = approved(rig)
    assert schedule_native_variant(rig.services, parked.id, parked.version) is R.PARKED
    assert rig.row(parked.id)[:3] == ("approved", None, None)
    failed = approved(rig)
    assert schedule_native_variant(rig.services, failed.id, failed.version) is R.FAILED
    assert "not allowed" in rig.reasons(failed.id)[-1]


def test_an_uncertain_schedule_is_never_repeated_because_a_copy_may_exist(
    conninfo: str, seed: Seed
) -> None:
    """The platform scheduled it, then the answer was lost. Scheduling again would post twice."""
    rig = rig_with(conninfo, seed, schedule=[("after", errors.UnknownOutcome("response lost"))])
    v = approved(rig)
    assert schedule_native_variant(rig.services, v.id, v.version) is R.FAILED

    assert len(held_ids(rig, v.id)) == 1  # the platform really is holding one
    reason = rig.reasons(v.id)[-1]
    assert (
        "uncertain whether the platform scheduled" in reason and "delete any copy by hand" in reason
    )
    assert rig.row(v.id)[1] is None  # nothing is scheduled to try again
    assert run_due(rig.services) == [] and rig.publisher.calls["schedule"] == 1


def test_a_run_that_dies_while_scheduling_is_failed_for_a_person_not_retried(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed, schedule=[("after", SimulatedCrash())])
    v = approved(rig)
    with pytest.raises(SimulatedCrash):
        schedule_native_variant(rig.services, v.id, v.version)
    assert rig.get(v.id).status is S.SCHEDULING_NATIVE

    rig.clock.advance(5 * MIN)
    assert fail_stale_scheduling(rig.services) == []  # still fresh
    rig.clock.advance(6 * MIN)
    assert fail_stale_scheduling(rig.services) == [v.id]
    assert rig.get(v.id).status is S.FAILED and "scheduled copy" in rig.reasons(v.id)[-1]
    assert rig.attempts(v.id) == [("schedule_native", "unknown")]  # the dangling intent is closed
    assert len(held_ids(rig, v.id)) == 1 and rig.publisher.calls["schedule"] == 1


# --- the scheduler ticking, end to end --------------------------------------------------------


def test_a_day_with_one_native_and_one_direct_post_each_goes_out_once(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    native, direct = approved(rig), approved(rig, delivery="direct")
    end = SLOT + 30 * MIN
    while rig.clock.now() < end:
        run_due(rig.services)
        rig.clock.advance(timedelta(minutes=1))

    for v in (native, direct):
        assert rig.get(v.id).status is S.PUBLISHED and len(held_ids(rig, v.id)) == 1
    # Only the direct post was published by this system; the native one by the platform itself.
    assert rig.publisher.calls["publish"] == 1 and rig.publisher.calls["schedule"] == 1


def test_run_action_dispatches_the_schedule_native_action(conninfo: str, seed: Seed) -> None:
    from dk_publishing.application.ports import DueAction

    rig = rig_with(conninfo, seed)
    v = approved(rig)
    due = DueAction(v.id, Action.SCHEDULE_NATIVE, T0, v.version, "p", v.account_id)
    assert run_action(rig.services, due) is R.SCHEDULED


# --- taking a post back from the platform -----------------------------------------------------


def test_withdrawing_removes_the_held_post_from_the_platform(conninfo: str, seed: Seed) -> None:
    rig = rig_with(conninfo, seed)
    v = scheduled(rig)
    assert withdraw_native(rig.services, v, rig.clock.now()) is True
    assert held_ids(rig, v.id) == [] and rig.publisher.calls["cancel"] == 1


def test_a_post_past_its_slot_is_not_cancelled_because_it_may_already_be_live(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    v = scheduled(rig)
    assert withdraw_native(rig.services, v, SLOT) is False
    assert len(held_ids(rig, v.id)) == 1 and rig.publisher.calls["cancel"] == 0


def test_withdrawing_something_not_scheduled_does_nothing(conninfo: str, seed: Seed) -> None:
    rig = rig_with(conninfo, seed)
    v = approved(rig)
    assert withdraw_native(rig.services, v, rig.clock.now()) is True
    assert rig.publisher.calls["cancel"] == 0


def test_a_platform_error_while_withdrawing_leaves_the_post_scheduled(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed, cancel=[errors.Retryable("platform down")])
    v = scheduled(rig)
    with pytest.raises(errors.Retryable):
        withdraw_native(rig.services, v, rig.clock.now())
    assert len(held_ids(rig, v.id)) == 1  # still held; the caller can try again


def test_a_scheduled_variant_without_its_handle_is_an_integrity_error(
    conninfo: str, seed: Seed
) -> None:
    rig = rig_with(conninfo, seed)
    v = scheduled(rig)
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "UPDATE publishing.variants SET native_handle = NULL WHERE id = %s::uuid", (v.id,)
        )
    with pytest.raises(IntegrityError, match="no handle to cancel"):
        withdraw_native(rig.services, v, rig.clock.now())
