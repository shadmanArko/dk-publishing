"""What the Sheet sync must do about a post the platform is already holding."""

from __future__ import annotations

from datetime import timedelta

from dk_publishing.application.use_cases.schedule_native import schedule_native_variant
from dk_publishing.domain import errors
from dk_publishing.domain.status import VariantStatus
from tests.integration.conftest import TENANT, Seed
from tests.integration.rig import R
from tests.integration.sync_rig import SyncRig
from tests.support import MIN, NATIVE_CAPS, T0

S = VariantStatus


def native_rig(conninfo: str, seed: Seed, **script: object) -> SyncRig:
    rig = SyncRig(conninfo, seed, caps=NATIVE_CAPS, **script)
    rig.post_with_row()
    rig.sheet.row("DK-1")["options"] = {"format": "reel", "delivery": "native"}
    return rig


def hand_over(rig: SyncRig) -> None:
    """Sync, then let the scheduler give the post to the (dry-run) platform."""
    rig.sync()
    v = rig.only().variant
    assert schedule_native_variant(rig.services, v.id, v.version) is R.SCHEDULED


def held(rig: SyncRig) -> int:
    return len(rig.ledger.posts_for(TENANT, rig.only().variant.id))


def test_a_native_row_is_approved_and_waits_to_be_handed_over(conninfo: str, seed: Seed) -> None:
    rig = native_rig(conninfo, seed)
    assert rig.sync().approved == 1
    v = rig.only().variant
    assert v.status is S.APPROVED and rig.row(v.id)[1] == "schedule_native"
    with rig.services.uow() as uow:
        assert uow.variants.snapshot_of(v.id)["delivery"] == "native"  # type: ignore[index]


def test_editing_a_post_the_platform_holds_takes_it_back_first(conninfo: str, seed: Seed) -> None:
    rig = native_rig(conninfo, seed)
    hand_over(rig)
    assert held(rig) == 1

    rig.sheet.row("DK-1")["caption"] = "A better caption"
    report = rig.sync()

    assert (report.withdrawn, report.approved) == (1, 1) and not report.errors
    assert held(rig) == 0  # the old copy is gone from the platform, so it cannot go out
    assert rig.only().variant.status is S.APPROVED
    assert rig.row(rig.only().variant.id)[1] == "schedule_native"  # and the new one is queued


def test_unticking_ready_takes_the_post_back_from_the_platform(conninfo: str, seed: Seed) -> None:
    rig = native_rig(conninfo, seed)
    hand_over(rig)
    rig.sheet.post("DK-1")["ready"] = False
    assert rig.sync().withdrawn == 1
    assert held(rig) == 0 and rig.only().variant.status is S.DRAFT


def test_deleting_the_row_takes_the_post_back_and_cancels_it(conninfo: str, seed: Seed) -> None:
    rig = native_rig(conninfo, seed)
    hand_over(rig)
    rig.sheet.delete_rows("DK-1")
    assert rig.sync().cancelled == 1
    assert held(rig) == 0 and rig.only().variant.status is S.CANCELLED


def test_switching_the_row_off_takes_the_post_back_and_cancels_it(
    conninfo: str, seed: Seed
) -> None:
    rig = native_rig(conninfo, seed)
    hand_over(rig)
    rig.sheet.row("DK-1")["enabled"] = False
    assert rig.sync().cancelled == 1 and held(rig) == 0


def test_switching_from_native_to_direct_withdraws_the_held_copy_and_publishes_directly(
    conninfo: str, seed: Seed
) -> None:
    rig = native_rig(conninfo, seed)
    hand_over(rig)
    rig.sheet.row("DK-1")["options"] = {"format": "reel", "delivery": "direct"}
    report = rig.sync()
    assert (report.withdrawn, report.approved) == (1, 1) and held(rig) == 0
    assert rig.row(rig.only().variant.id)[1] == "prepare"


def test_once_the_slot_has_arrived_nothing_is_taken_back(conninfo: str, seed: Seed) -> None:
    """The platform may already have published it; deleting a live post is out of scope."""
    rig = native_rig(conninfo, seed)
    hand_over(rig)
    rig.clock.set(T0 + timedelta(hours=5, minutes=1))  # a minute past the slot
    rig.sheet.row("DK-1")["caption"] = "Too late"
    report = rig.sync()

    assert report.busy == 1 and not report.changed and not report.errors
    assert rig.only().variant.status is S.SCHEDULED_NATIVE and held(rig) == 1
    assert rig.publisher.calls["cancel"] == 0


def test_if_the_platform_cannot_take_it_back_the_post_stays_and_the_next_sync_retries(
    conninfo: str, seed: Seed
) -> None:
    rig = native_rig(conninfo, seed, cancel=[errors.Retryable("platform down")])
    hand_over(rig)
    rig.sheet.row("DK-1")["caption"] = "A better caption"

    first = rig.sync()
    assert len(first.errors) == 1 and "platform down" in first.errors[0]
    assert rig.only().variant.status is S.SCHEDULED_NATIVE and held(rig) == 1  # untouched

    second = rig.sync()  # the platform is back
    assert not second.errors and second.withdrawn == 1 and held(rig) == 0


def test_six_scheduled_posts_removed_at_once_still_trigger_the_guard(
    conninfo: str, seed: Seed
) -> None:
    rig = SyncRig(conninfo, seed, caps=NATIVE_CAPS)
    for i in range(1, 7):
        rig.post_with_row(f"DK-{i}")
        rig.sheet.row(f"DK-{i}")["options"] = {"format": "reel", "delivery": "native"}
    rig.sync()
    for v in rig.variants():
        assert schedule_native_variant(rig.services, v.variant.id, v.variant.version) is R.SCHEDULED
    for i in range(1, 7):
        rig.sheet.delete_rows(f"DK-{i}")

    report = rig.sync()
    assert report.halted and "6 scheduled posts" in report.halted
    assert (
        len(rig.ledger.posts_for(TENANT, rig.variants()[0].variant.id)) == 1
    )  # nothing was touched


# --- problems are explained before anything is approved -------------------------------------------


def test_native_too_close_to_the_slot_is_explained(conninfo: str, seed: Seed) -> None:
    rig = SyncRig(conninfo, seed, caps=NATIVE_CAPS)
    rig.post_with_row(slot_in=0.1)  # six minutes away
    rig.sheet.row("DK-1")["options"] = {"format": "reel", "delivery": "native"}
    rig.sync()
    assert rig.only().variant.status is S.INVALID
    assert "at least 10 minutes from now" in rig.sheet.status("DK-1")["last_error"]


def test_native_on_a_platform_that_cannot_hold_posts_is_explained(
    conninfo: str, seed: Seed
) -> None:
    rig = SyncRig(conninfo, seed)  # the default platform has no native window
    rig.post_with_row()
    rig.sheet.row("DK-1")["options"] = {"format": "reel", "delivery": "native"}
    rig.sync()
    assert "cannot hold scheduled posts" in rig.sheet.status("DK-1")["last_error"]


def test_an_unknown_delivery_value_is_explained(conninfo: str, seed: Seed) -> None:
    rig = SyncRig(conninfo, seed, caps=NATIVE_CAPS)
    rig.post_with_row()
    rig.sheet.row("DK-1")["options"] = {"format": "reel", "delivery": "teleport"}
    rig.sync()
    assert "delivery must be one of: direct, native" in rig.sheet.status("DK-1")["last_error"]


def test_a_native_post_waiting_in_the_queue_is_not_made_invalid_as_time_passes(
    conninfo: str, seed: Seed
) -> None:
    rig = native_rig(conninfo, seed)
    rig.sync()
    rig.clock.set(
        T0 + timedelta(hours=4, minutes=55)
    )  # five minutes to the slot: native no longer possible
    report = rig.sync()
    assert not report.changed and rig.only().variant.status is S.APPROVED
    assert MIN
