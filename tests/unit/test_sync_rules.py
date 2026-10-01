from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dk_publishing.domain.publishing import Violation
from dk_publishing.domain.sheet import MediaFile, PlatformRow, PostRow
from dk_publishing.domain.status import ALLOWED, VariantStatus
from dk_publishing.domain.sync import (
    BUSY,
    CANCELLABLE,
    LIVE_EDIT_NOTE,
    SCHEDULED,
    cancel_path,
    content_for,
    effective_caption,
    effective_slot,
    parse_media,
    plan_steps,
    post_summary,
    resolve_media,
    resolve_slot,
    row_status,
    source_hash,
)
from dk_publishing.domain.timezones import local_to_serial, serial_to_local

S = VariantStatus
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def steps(existing: VariantStatus | None, **kw: bool) -> list[str]:
    base = {"changed": False, "enabled": True, "ready": True, "valid": True}
    return [s.value for s in plan_steps(existing, **{**base, **kw})]


# --- the decision table -----------------------------------------------------------------------


def test_a_new_ready_valid_row_is_created_and_approved() -> None:
    assert steps(None) == ["create", "approve"]


def test_a_new_row_that_is_not_ready_waits_as_a_draft() -> None:
    assert steps(None, ready=False) == ["create"]


def test_a_new_invalid_row_is_created_and_marked_invalid() -> None:
    assert steps(None, valid=False) == ["create", "mark_invalid"]


def test_a_new_row_that_is_switched_off_creates_nothing() -> None:
    assert steps(None, enabled=False) == []


def test_unticking_enabled_cancels_whatever_can_still_be_stopped() -> None:
    for status in CANCELLABLE:
        assert steps(status, enabled=False) == ["cancel"], status
    for status in (S.FAILED, S.CANCELLED, S.EXPIRED):
        assert steps(status, enabled=False) == [], status


def test_a_live_post_ignores_everything() -> None:
    for changed in (False, True):
        for enabled in (False, True):
            assert steps(S.PUBLISHED, changed=changed, enabled=enabled) == ["leave_live"]


def test_a_variant_someone_else_owns_is_left_for_the_next_sync() -> None:
    for status in BUSY:
        assert steps(status, changed=True) == ["busy"], status
        assert steps(status, enabled=False) == ["busy"], status


@pytest.mark.parametrize("status", sorted(SCHEDULED))
def test_an_edit_after_approval_withdraws_it_and_reapproves_if_still_ready(status: S) -> None:
    assert steps(status, changed=True) == ["reset_to_draft", "approve"]


@pytest.mark.parametrize("status", sorted(SCHEDULED))
def test_unticking_ready_after_approval_withdraws_it_and_stays_a_draft(status: S) -> None:
    assert steps(status, changed=True, ready=False) == ["reset_to_draft"]


@pytest.mark.parametrize("status", sorted(SCHEDULED))
def test_an_unchanged_approved_variant_is_left_alone_even_if_the_row_would_now_be_invalid(
    status: S,
) -> None:
    assert steps(status, valid=False) == []  # e.g. its slot has since passed while it waits


@pytest.mark.parametrize("status", [S.FAILED, S.CANCELLED, S.EXPIRED])
def test_a_finished_variant_stays_finished_until_the_row_is_edited(status: S) -> None:
    assert steps(status) == []
    assert steps(status, changed=True) == ["reset_to_draft", "approve"]


def test_an_edited_draft_just_records_its_new_inputs() -> None:
    assert steps(S.DRAFT, changed=True, ready=False) == ["update_inputs"]
    assert steps(S.DRAFT, changed=True) == ["update_inputs", "approve"]


def test_an_edit_that_breaks_a_draft_marks_it_invalid() -> None:
    assert steps(S.DRAFT, changed=True, valid=False) == ["update_inputs", "mark_invalid"]


def test_fixing_an_invalid_variant_returns_it_to_draft_and_approves() -> None:
    assert steps(S.INVALID, changed=True) == ["update_inputs", "to_draft", "approve"]
    assert steps(S.INVALID, changed=True, ready=False) == ["update_inputs", "to_draft"]


def test_an_invalid_variant_that_is_still_invalid_changes_nothing() -> None:
    assert steps(S.INVALID, valid=False) == []
    assert steps(S.INVALID, changed=True, valid=False) == ["update_inputs"]


def test_an_unchanged_valid_draft_that_is_ready_gets_approved() -> None:
    assert steps(S.DRAFT) == ["approve"]  # picks up where an interrupted sync stopped
    assert steps(S.DRAFT, ready=False) == []


def test_every_planned_path_is_a_legal_lifecycle_move() -> None:
    """The steps are only names; check the moves they imply exist in the state machine."""
    for status in S:
        if status in CANCELLABLE:
            path, here = cancel_path(status), status
            for nxt in path:
                assert nxt in ALLOWED[here], (status, nxt)
                here = nxt
            assert here is S.CANCELLED


# --- slots ------------------------------------------------------------------------------------


def test_a_future_berlin_slot_becomes_a_utc_instant() -> None:
    utc, problems = resolve_slot(datetime(2026, 11, 14, 18, 0), NOW)
    assert utc == datetime(2026, 11, 14, 17, 0, tzinfo=UTC) and problems == []


def test_a_missing_slot_says_where_to_put_one() -> None:
    utc, [problem] = resolve_slot(None, NOW)
    assert utc is None and "default_slot" in problem.message


def test_a_slot_in_the_past_is_still_resolved_but_flagged() -> None:
    utc, [problem] = resolve_slot(datetime(2026, 9, 1, 18, 0), NOW)
    assert (
        utc is not None
        and "in the past" in problem.message
        and "01.09.2026 18:00" in problem.message
    )


@pytest.mark.parametrize(
    ("local", "words"),
    [
        (datetime(2026, 3, 29, 2, 30), "does not exist"),
        (datetime(2026, 10, 25, 2, 30), "happens twice"),
    ],
)
def test_clock_change_hours_are_rejected_not_guessed(local: datetime, words: str) -> None:
    utc, [problem] = resolve_slot(local, datetime(2026, 1, 1, tzinfo=UTC))
    assert utc is None and words in problem.message


def test_sheet_serials_round_trip() -> None:
    local = datetime(2026, 11, 14, 18, 0)
    assert local_to_serial(local) == pytest.approx(46340.75)
    assert serial_to_local(46340.75) == local
    assert serial_to_local(local_to_serial(datetime(2026, 3, 1, 9, 5, 7))) == datetime(
        2026, 3, 1, 9, 5, 7
    )


# --- media ------------------------------------------------------------------------------------

FILES = [
    MediaFile("f1", "eid_platter.mp4", "md5-a"),
    MediaFile("f2", "dup.jpg", "md5-b"),
    MediaFile("f3", "dup.jpg", "md5-c"),
]


def test_media_names_are_split_trimmed_and_blanks_dropped() -> None:
    assert parse_media(" a.mp4 , b.jpg,, ") == ("a.mp4", "b.jpg")
    assert parse_media("") == ()


def test_found_files_resolve_to_their_id_and_checksum() -> None:
    resolved, problems = resolve_media(["eid_platter.mp4"], FILES)
    assert problems == [] and (resolved[0].file_id, resolved[0].md5) == ("f1", "md5-a")


def test_a_missing_file_and_a_duplicated_name_are_each_explained() -> None:
    resolved, problems = resolve_media(["nope.mp4", "dup.jpg"], FILES)
    assert [r.file_id for r in resolved] == [None, None]
    assert "'nope.mp4' was not found" in problems[0].message
    assert "2 files named 'dup.jpg'" in problems[1].message


# --- what counts as an edit -------------------------------------------------------------------


def post(**over: object) -> PostRow:
    base = dict(
        row=2, post_key="DK-1", title="Eid", media=("eid_platter.mp4",), default_caption="Hi",
        default_slot=datetime(2026, 11, 14, 18, 0), ready=True,
    )  # fmt: skip
    return PostRow(**{**base, **over})  # type: ignore[arg-type]


def platform(**over: object) -> PlatformRow:
    base = dict(
        tab="T", platform="p", row=2, post_key="DK-1", enabled=True, account="Acc",
        caption="", slot=None, options={"format": "reel"},
    )  # fmt: skip
    return PlatformRow(**{**base, **over})  # type: ignore[arg-type]


MEDIA = resolve_media(["eid_platter.mp4"], FILES)[0]


def hash_of(p: PostRow | None = None, r: PlatformRow | None = None, media=MEDIA) -> str:  # type: ignore[no-untyped-def]
    return source_hash(p or post(), r or platform(), media)


def test_the_same_inputs_always_hash_the_same() -> None:
    assert hash_of() == hash_of(post(row=99), platform(row=7))  # row numbers are not content


@pytest.mark.parametrize(
    ("changed_post", "changed_row"),
    [
        ({"ready": False}, {}),
        ({"default_caption": "Hello"}, {}),
        ({"default_slot": datetime(2026, 11, 15, 18, 0)}, {}),
        ({"media": ("other.mp4",)}, {}),
        ({}, {"enabled": False}),
        ({}, {"account": "Other"}),
        ({}, {"caption": "Own caption"}),
        ({}, {"slot": datetime(2026, 12, 1, 9, 0)}),
        ({}, {"options": {"format": "feed"}}),
    ],
)
def test_any_relevant_edit_changes_the_hash(
    changed_post: dict[str, object], changed_row: dict[str, object]
) -> None:
    assert hash_of(post(**changed_post), platform(**changed_row)) != hash_of()


def test_a_retitled_post_keeps_its_approval() -> None:
    assert hash_of(post(title="A better name")) == hash_of()


def test_a_replaced_drive_file_changes_the_hash_even_though_the_name_did_not() -> None:
    replaced = resolve_media(["eid_platter.mp4"], [MediaFile("f1", "eid_platter.mp4", "md5-NEW")])[
        0
    ]
    assert hash_of(media=replaced) != hash_of()


def test_account_names_ignore_case_and_spaces_for_hashing() -> None:
    assert hash_of(r=platform(account=" acc ")) == hash_of()


def test_platform_values_win_over_post_defaults() -> None:
    p, r = post(), platform(caption="Specific", slot=datetime(2026, 12, 1, 9, 0))
    assert effective_caption(p, r) == "Specific" and effective_slot(p, r) == datetime(
        2026, 12, 1, 9, 0
    )
    assert (
        effective_caption(p, platform()) == "Hi" and effective_slot(p, platform()) == p.default_slot
    )
    assert effective_caption(None, platform()) == "" and effective_slot(None, platform()) is None


def test_the_frozen_content_has_caption_media_and_options_but_not_the_slot() -> None:
    content = content_for(post(), platform(), MEDIA)
    assert content == {
        "caption": "Hi",
        "media": [{"name": "eid_platter.mp4", "drive_file_id": "f1", "md5": "md5-a"}],
        "format": "reel",
    }


# --- what the person reads --------------------------------------------------------------------

BAD = [Violation("caption", "Caption is empty."), Violation("slot", "The slot is in the past.")]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(enabled=True, variant_status=None, problems=BAD), ("invalid", "", "Caption is empty. The slot is in the past.")),
        (dict(enabled=False, variant_status=None, problems=BAD), ("", "", "")),
        (dict(enabled=True, variant_status=None, problems=[]), ("", "", "")),
        (dict(enabled=True, variant_status=S.DRAFT, problems=[]), ("draft", "", "")),
        (dict(enabled=True, variant_status=S.INVALID, problems=BAD), ("invalid", "", "Caption is empty. The slot is in the past.")),
        (dict(enabled=True, variant_status=S.APPROVED, problems=BAD), ("approved", "", "")),
        (dict(enabled=True, variant_status=S.FAILED, problems=[], last_reason="platform rejected it: policy 4.2"), ("failed", "", "platform rejected it: policy 4.2")),
        (dict(enabled=True, variant_status=S.EXPIRED, problems=[], last_reason="past max lateness"), ("expired", "", "past max lateness")),
        (dict(enabled=False, variant_status=S.CANCELLED, problems=[], last_reason="x"), ("cancelled", "", "")),
        (dict(enabled=True, variant_status=S.PUBLISHED, problems=[], external_url="https://x/1"), ("published", "https://x/1", "")),
        (dict(enabled=True, variant_status=S.PUBLISHED, problems=[], external_url="https://x/1", edited_while_live=True), ("published", "https://x/1", LIVE_EDIT_NOTE)),
    ],
)  # fmt: skip
def test_row_status_wording(kwargs: dict[str, object], expected: tuple[str, str, str]) -> None:
    defaults: dict[str, object] = {
        "external_url": None,
        "last_reason": None,
        "edited_while_live": False,
    }
    assert row_status(**{**defaults, **kwargs}) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("statuses", "summary"),
    [
        ([], ""),
        (["", "cancelled"], ""),
        (["published", "published", "approved"], "2 of 3 live"),
        (
            ["published", "failed", "expired", "invalid", "draft"],
            "1 of 5 live, 2 failed, 1 invalid, 1 waiting",
        ),
        (["approved"], "0 of 1 live"),
    ],
)
def test_the_posts_roll_up(statuses: list[str], summary: str) -> None:
    assert post_summary(statuses) == summary


def test_a_week_of_slots_never_crosses_a_day_boundary_wrongly() -> None:
    start = datetime(2026, 11, 1, 0, 0)
    for day in range(60):
        local = start + timedelta(days=day, hours=18)
        utc, problems = resolve_slot(local, datetime(2026, 1, 1, tzinfo=UTC))
        assert not problems and utc is not None
