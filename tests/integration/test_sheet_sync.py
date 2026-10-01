"""The Sheet sync end to end: typed rows in, Postgres and status cells out."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
import pytest

from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.domain import errors
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.sync import LIVE_EDIT_NOTE
from tests.integration.conftest import Seed
from tests.integration.rig import R
from tests.integration.sync_rig import SyncRig, berlin_in
from tests.support import T0, SimulatedCrash
from tests.support.memory_sheet import FakeMedia

S = VariantStatus


@pytest.fixture
def rig(conninfo: str, seed: Seed) -> SyncRig:
    return SyncRig(conninfo, seed)


def events(rig: SyncRig, key: str = "DK-1") -> list[str]:
    with rig.services.uow() as uow:
        return [e.to_status.value for e in uow.variants.events(rig.only(key).variant.id)]


# --- a new row ---------------------------------------------------------------------------------


def test_a_ready_row_becomes_an_approved_variant_and_the_sheet_says_so(rig: SyncRig) -> None:
    rig.post_with_row(title="Eid platter")
    report = rig.sync()

    assert (report.created, report.approved) == (1, 1) and not report.errors
    found = rig.only()
    assert found.variant.status is S.APPROVED and found.source_hash
    assert found.variant.publish_at == T0 + timedelta(hours=5)  # 18:00 Berlin
    assert (
        rig.sheet.status("DK-1")["status"] == "approved"
        and not rig.sheet.status("DK-1")["last_error"]
    )
    assert rig.sheet.post_status("DK-1") == {"status": "0 of 1 live", "last_error": ""}
    assert [(e.post_key, e.status) for e in rig.sheet.calendar] == [("DK-1", "approved")]
    assert rig.sheet.accounts == {"p": ["Main"]}


def test_the_raw_rows_are_kept_as_an_audit_copy_when_something_changed(
    rig: SyncRig, conninfo: str
) -> None:
    rig.post_with_row()
    rig.sync()
    rig.sync()  # nothing changed: no second copy
    with psycopg.connect(conninfo) as conn:
        [(syncs, rows)] = conn.execute(
            "SELECT count(DISTINCT sync_id), count(*) FROM publishing.sheet_snapshots"
        ).fetchall()
    assert (syncs, rows) == (1, 2)


def test_syncing_again_changes_and_writes_nothing(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    before = rig.only().variant.version
    again = rig.sync()
    assert not again.changed and again.cells_written == 0 and not again.errors
    assert rig.only().variant.version == before


def test_a_row_that_is_not_ready_waits_as_a_draft_until_it_is_ticked(rig: SyncRig) -> None:
    rig.post_with_row(ready=False)
    rig.sync()
    assert rig.only().variant.status is S.DRAFT and rig.sheet.status("DK-1")["status"] == "draft"
    assert rig.sheet.post_status("DK-1")["status"] == "0 of 1 live, 1 waiting"

    rig.sheet.post("DK-1")["ready"] = True
    report = rig.sync()
    assert report.approved == 1 and rig.only().variant.status is S.APPROVED
    assert events(rig) == ["approved"]  # creating a draft is not a transition


def test_a_switched_off_row_creates_nothing(rig: SyncRig) -> None:
    rig.sheet.add_post("DK-1", default_slot=berlin_in(5), ready=True, media=("reel.mp4",))
    rig.sheet.add_row("DK-1", enabled=False)
    rig.sync()
    assert rig.variants() == [] and rig.sheet.status("DK-1") == {}


# --- problems are explained in plain words ----------------------------------------------------


def test_a_missing_media_file_is_invalid_and_fixing_it_approves_the_post(rig: SyncRig) -> None:
    rig.media.remove("reel.mp4")
    rig.post_with_row()
    rig.sync()
    assert rig.only().variant.status is S.INVALID
    assert (
        rig.sheet.status("DK-1")["last_error"]
        == "Media file 'reel.mp4' was not found in the Drive folder."
    )
    assert rig.sheet.post_status("DK-1")["last_error"].startswith("Platform P: Media file")

    rig.media.reset("reel.mp4")  # the file arrives in Drive; nobody touches the Sheet
    report = rig.sync()
    assert report.approved == 1 and rig.only().variant.status is S.APPROVED
    assert events(rig) == ["invalid", "draft", "approved"]
    assert rig.sheet.status("DK-1")["last_error"] == ""


def test_two_drive_files_with_one_name_are_refused(rig: SyncRig) -> None:
    rig.media.files += FakeMedia("reel.mp4").files  # a second file with the same name, other id
    rig.media.files[-1] = type(rig.media.files[0])("other-id", "reel.mp4", "md5-x")
    rig.post_with_row()
    rig.sync()
    assert "2 files named 'reel.mp4'" in rig.sheet.status("DK-1")["last_error"]


def test_a_slot_in_the_past_is_invalid(rig: SyncRig) -> None:
    rig.post_with_row(slot_in=-2)
    rig.sync()
    assert rig.only().variant.status is S.INVALID
    assert "in the past" in rig.sheet.status("DK-1")["last_error"]


def test_a_row_without_any_slot_cannot_become_a_variant_but_says_why(rig: SyncRig) -> None:
    rig.sheet.add_post("DK-1", ready=True, media=("reel.mp4",))
    rig.sheet.add_row("DK-1")
    rig.sync()
    assert rig.variants() == []
    status = rig.sheet.status("DK-1")
    assert status["status"] == "invalid" and "default_slot" in status["last_error"]


@pytest.mark.parametrize(
    ("local", "words"),
    [
        (datetime(2027, 3, 28, 2, 30), "does not exist"),
        (datetime(2027, 10, 31, 2, 30), "happens twice"),
    ],
)
def test_clock_change_times_are_rejected_in_the_sheet_not_guessed(
    rig: SyncRig, local: datetime, words: str
) -> None:
    rig.sheet.add_post("DK-1", default_slot=local, ready=True, media=("reel.mp4",))
    rig.sheet.add_row("DK-1")
    rig.sync()
    assert rig.variants() == [] and words in rig.sheet.status("DK-1")["last_error"]


def test_an_unknown_account_and_an_unknown_post_key_are_explained(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sheet.row("DK-1")["account"] = "Nobody"
    rig.sheet.add_row("DK-9")
    rig.sync()
    assert rig.variants() == []
    assert "Account 'Nobody' is not connected" in rig.sheet.status("DK-1")["last_error"]
    assert rig.sheet.status("DK-9")["last_error"].startswith(
        "post_key 'DK-9' is not on the Posts tab."
    )


def test_a_post_key_used_twice_on_posts_is_flagged_everywhere(rig: SyncRig) -> None:
    rig.sheet.add_post("DK-1", default_slot=berlin_in(5), ready=True, media=("reel.mp4",))
    rig.sheet.add_post("DK-1", default_slot=berlin_in(6), ready=True, media=("reel.mp4",))
    rig.sheet.add_row("DK-1")
    rig.sync()
    assert rig.variants() == []
    assert "appears more than once on Posts" in rig.sheet.status("DK-1")["last_error"]
    assert "appears more than once" in rig.sheet.post_status("DK-1")["last_error"]


def test_cell_problems_found_while_reading_are_passed_through(rig: SyncRig) -> None:
    from tests.support.memory_sheet import problem

    rig.post_with_row()
    rig.sheet.row("DK-1")["problems"] = (problem("format", "format must be one of: feed, reel."),)
    rig.sync()
    assert rig.only().variant.status is S.INVALID
    assert rig.sheet.status("DK-1")["last_error"] == "format must be one of: feed, reel."


def test_sheet_level_problems_reach_the_report(rig: SyncRig) -> None:
    rig.sheet.tab_problems = ["Tab 'X' is missing column(s) format. Run `dk sheet init`."]
    assert rig.sync().problems == rig.sheet.tab_problems


# --- edits after approval ---------------------------------------------------------------------


def approved_and_prepared(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    v = rig.only().variant
    assert prepare_variant(rig.services, v.id, v.version) is R.PREPARED


def test_editing_a_prepared_post_withdraws_the_approval_and_approves_the_new_version(
    rig: SyncRig,
) -> None:
    approved_and_prepared(rig)
    rig.sheet.row("DK-1")["caption"] = "A better caption"
    report = rig.sync()

    assert (report.withdrawn, report.approved) == (1, 1)
    found = rig.only()
    assert found.variant.status is S.APPROVED
    assert events(rig) == ["approved", "preparing", "prepared", "draft", "approved"]
    with rig.services.uow() as uow:
        assert uow.variants.snapshot_of(found.variant.id)["caption"] == "A better caption"  # type: ignore[index]
        assert uow.variants.handle_of(found.variant.id) is None  # the old prepared handle is gone
    assert rig.row(found.variant.id)[1] == "prepare"  # scheduled to be prepared again


def test_unticking_ready_withdraws_the_approval_and_leaves_a_draft(rig: SyncRig) -> None:
    approved_and_prepared(rig)
    rig.sheet.post("DK-1")["ready"] = False
    report = rig.sync()
    assert (report.withdrawn, report.approved) == (1, 0)
    assert rig.only().variant.status is S.DRAFT and rig.sheet.status("DK-1")["status"] == "draft"
    assert rig.row(rig.only().variant.id)[1] is None  # nothing is scheduled


def test_replacing_the_drive_file_withdraws_and_reapproves_with_the_new_checksum(
    rig: SyncRig,
) -> None:
    rig.post_with_row()
    rig.sync()
    rig.media.replace("reel.mp4", "md5-NEW")
    report = rig.sync()
    assert (report.withdrawn, report.approved) == (1, 1)
    with rig.services.uow() as uow:
        snapshot = uow.variants.snapshot_of(rig.only().variant.id)
    assert snapshot["media"][0]["md5"] == "md5-NEW"  # type: ignore[index]


def test_retitling_a_post_does_not_withdraw_its_approval(rig: SyncRig) -> None:
    rig.post_with_row(title="Old name")
    rig.sync()
    version = rig.only().variant.version
    rig.sheet.post("DK-1")["title"] = "New name"
    report = rig.sync()
    assert not report.changed and rig.only().variant.version == version
    assert rig.only().title == "New name"


def test_a_live_post_ignores_edits_and_says_so(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    v = rig.only().variant
    prepare_variant(rig.services, v.id, v.version)
    rig.clock.set(T0 + timedelta(hours=5))
    prepared = rig.only().variant
    assert publish_variant(rig.services, v.id, prepared.version) is R.PUBLISHED

    rig.sync()
    assert (
        rig.sheet.status("DK-1")["status"] == "published" and rig.sheet.status("DK-1")["live_url"]
    )
    assert rig.sheet.status("DK-1")["last_error"] == ""
    assert rig.sheet.post_status("DK-1")["status"] == "1 of 1 live"

    rig.sheet.row("DK-1")["caption"] = "Too late to change"
    report = rig.sync()
    assert report.left_live == 1 and not report.changed
    assert rig.sheet.status("DK-1")["last_error"] == LIVE_EDIT_NOTE
    assert rig.only().variant.status is S.PUBLISHED


def test_a_failed_post_stays_failed_until_the_row_is_edited(conninfo: str, seed: Seed) -> None:
    rig = SyncRig(conninfo, seed, publish=[errors.Rejected("caption violates policy 4.2")])
    rig.post_with_row()
    rig.sync()
    v = rig.only().variant
    prepare_variant(rig.services, v.id, v.version)
    rig.clock.set(T0 + timedelta(hours=5))
    assert publish_variant(rig.services, v.id, rig.only().variant.version) is R.FAILED

    rig.sync()
    assert rig.sheet.status("DK-1")["status"] == "failed"
    assert "caption violates policy 4.2" in rig.sheet.status("DK-1")["last_error"]
    assert not rig.sync().changed  # unchanged row: it is not retried behind your back
    assert rig.only().variant.status is S.FAILED

    rig.sheet.post("DK-1")["default_slot"] = berlin_in(10)  # fix: a new slot and a new caption
    rig.sheet.row("DK-1")["caption"] = "A friendlier caption"
    report = rig.sync()
    assert report.approved == 1 and rig.only().variant.status is S.APPROVED


def test_a_variant_that_is_mid_publish_is_left_alone_until_it_settles(
    conninfo: str, seed: Seed
) -> None:
    rig = SyncRig(conninfo, seed, publish=[("after", SimulatedCrash())])
    rig.post_with_row()
    rig.sync()
    v = rig.only().variant
    prepare_variant(rig.services, v.id, v.version)
    rig.clock.set(T0 + timedelta(hours=5))
    with pytest.raises(SimulatedCrash):
        publish_variant(rig.services, v.id, rig.only().variant.version)

    rig.sheet.row("DK-1")["caption"] = "edited during the publish"
    report = rig.sync()
    assert report.busy == 1 and not report.changed
    assert rig.only().variant.status is S.PUBLISHING


# --- cancelling -------------------------------------------------------------------------------


def test_unticking_enabled_cancels_the_post(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    rig.sheet.row("DK-1")["enabled"] = False
    assert rig.sync().cancelled == 1
    assert (
        rig.only().variant.status is S.CANCELLED
        and rig.sheet.status("DK-1")["status"] == "cancelled"
    )
    assert rig.sheet.post_status("DK-1")["status"] == ""
    assert [e.status for e in rig.sheet.calendar] == []


def test_deleting_the_row_cancels_it_and_an_invalid_one_passes_through_draft(rig: SyncRig) -> None:
    rig.media.remove("reel.mp4")
    rig.post_with_row()
    rig.sync()  # invalid
    rig.sheet.delete_rows("DK-1")
    assert rig.sync().cancelled == 1
    assert rig.only().variant.status is S.CANCELLED
    assert events(rig) == ["invalid", "draft", "cancelled"]


def test_unticking_and_reticking_enabled_brings_the_post_back(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    rig.sheet.row("DK-1")["enabled"] = False
    rig.sync()
    rig.sheet.row("DK-1")["enabled"] = True
    report = rig.sync()
    assert report.approved == 1 and rig.only().variant.status is S.APPROVED


def posts(rig: SyncRig, n: int) -> list[str]:
    keys = [f"DK-{i}" for i in range(1, n + 1)]
    for key in keys:
        rig.post_with_row(key)
    return keys


def test_deleting_a_block_of_rows_halts_the_sync_and_changes_nothing(rig: SyncRig) -> None:
    keys = posts(rig, 6)
    rig.sync()
    before = rig.statuses()
    plans = len(rig.sheet.plans)
    for key in keys:
        rig.sheet.delete_rows(key)

    report = rig.sync()

    assert (
        report.halted
        and "6 scheduled posts" in report.halted
        and "Nothing was changed" in report.halted
    )
    assert rig.statuses() == before and all(s == "approved" for s in before.values())
    assert len(rig.sheet.plans) == plans  # not even a status write


def test_the_guard_can_be_overridden_on_purpose(rig: SyncRig) -> None:
    keys = posts(rig, 6)
    rig.sync()
    for key in keys:
        rig.sheet.delete_rows(key)
    report = rig.sync(allow_cancellations=True)
    assert report.cancelled == 6 and not report.halted
    assert set(rig.statuses().values()) == {"cancelled"}


def test_five_cancellations_are_still_ordinary(rig: SyncRig) -> None:
    keys = posts(rig, 5)
    rig.sync()
    for key in keys:
        rig.sheet.delete_rows(key)
    assert rig.sync().cancelled == 5


def test_drafts_do_not_count_toward_the_guard(rig: SyncRig) -> None:
    for i in range(1, 9):
        rig.post_with_row(f"DK-{i}", ready=False)
    rig.sync()
    for i in range(1, 9):
        rig.sheet.delete_rows(f"DK-{i}")
    report = rig.sync()
    assert report.cancelled == 8 and not report.halted


# --- awkward sheets ---------------------------------------------------------------------------


def test_the_same_row_twice_is_flagged_and_nothing_is_cancelled(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    rig.sheet.add_row("DK-1")  # an accidental copy-paste of the same post, platform and account
    report = rig.sync()
    assert report.cancelled == 0 and rig.only().variant.status is S.APPROVED
    for index in (0, 1):
        assert "listed twice" in rig.sheet.status("DK-1", index=index)["last_error"]


def test_changing_the_account_moves_the_post_to_a_new_variant(rig: SyncRig) -> None:
    rig.add_account("Second")
    rig.post_with_row()
    rig.sync()
    rig.sheet.row("DK-1")["account"] = "second"  # case does not matter
    report = rig.sync()
    assert (report.created, report.cancelled, report.approved) == (1, 1, 1)
    assert sorted(v.variant.status.value for v in rig.variants()) == ["approved", "cancelled"]


def test_one_variant_that_cannot_be_updated_does_not_stop_the_others(
    rig: SyncRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = approve

    def flaky(services, variant_id, content, actor):  # type: ignore[no-untyped-def]
        if content["caption"] == "BOOM":
            raise RuntimeError("database fell over")
        return real(services, variant_id, content, actor)

    monkeypatch.setattr("dk_publishing.application.use_cases.sync_sheet.approve", flaky)
    rig.post_with_row("DK-1", default_caption="BOOM")
    rig.post_with_row("DK-2")
    report = rig.sync()
    assert len(report.errors) == 1 and "RuntimeError: database fell over" in report.errors[0]
    assert rig.statuses() == {"DK-1": "draft", "DK-2": "approved"}
    monkeypatch.setattr("dk_publishing.application.use_cases.sync_sheet.approve", real)
    assert rig.sync().approved == 1  # and the next sync picks the failed one up


def test_only_scheduled_posts_appear_on_the_calendar(rig: SyncRig) -> None:
    rig.post_with_row("DK-1")
    rig.post_with_row("DK-2", ready=False)
    rig.sync()
    assert [e.post_key for e in rig.sheet.calendar] == ["DK-1"]


def test_the_account_dropdown_lists_every_active_account(rig: SyncRig) -> None:
    rig.add_account("Zed")
    rig.sync()
    assert rig.sheet.accounts == {"p": ["Main", "Zed"]}


def test_a_publisher_that_is_switched_off_says_so(conninfo: str, seed: Seed) -> None:
    rig = SyncRig(conninfo, seed)
    rig.post_with_row()
    rig.sheet.row("DK-1")["platform"] = "off"
    rig.sync()
    assert "switched off" in rig.sheet.status("DK-1")["last_error"]


def test_a_deleted_row_that_is_put_back_revives_the_post(rig: SyncRig) -> None:
    rig.post_with_row()
    rig.sync()
    rig.sheet.delete_rows("DK-1")
    rig.sync()
    assert rig.only().variant.status is S.CANCELLED
    rig.sheet.add_row("DK-1")  # exactly the same row again
    report = rig.sync()
    assert report.approved == 1 and rig.only().variant.status is S.APPROVED
