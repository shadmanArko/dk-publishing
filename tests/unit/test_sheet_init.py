from __future__ import annotations

from typing import Any

import pytest
from tests.support.fake_sheets import FakeSheets

from dk_publishing.adapters.config.platforms import load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.adapters.sheets import sheet_init
from dk_publishing.adapters.sheets.sheet_init import (
    HeaderError,
    InitReport,
    build_tabs,
    col_letter,
    initialise,
    merge_header,
)
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG, DEFAULT_SHEET_CONFIG

PLATFORMS = load_platforms(DEFAULT_PLATFORMS_CONFIG)
LAYOUT = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(PLATFORMS))
TABS = build_tabs(LAYOUT, PLATFORMS)
TITLES = [t.title for t in TABS]
STATUS_COLUMNS = 10  # Posts, eight platform tabs, Calendar


def run(fake: FakeSheets, *, dry_run: bool = False) -> InitReport:
    return initialise(fake, "SID", LAYOUT, PLATFORMS, dry_run=dry_run)


def tab(name: str) -> Any:
    return next(t for t in TABS if t.title == name)


# --- helpers ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "letters"),
    [(0, "A"), (1, "B"), (25, "Z"), (26, "AA"), (51, "AZ"), (701, "ZZ"), (702, "AAA")],
)
def test_column_letters(index: int, letters: str) -> None:
    assert col_letter(index) == letters


def test_merge_header_keeps_positions_and_appends_what_is_missing() -> None:
    final, added = merge_header(["title", "mine", "post_key"], ["post_key", "title", "media"])
    assert (final, added) == (["title", "mine", "post_key", "media"], ["media"])
    assert merge_header([], ["a", "b"]) == (["a", "b"], ["a", "b"])
    with pytest.raises(HeaderError):
        merge_header(["a", "a"], ["a"])


def test_the_tabs_are_the_plans_thirteen_in_order() -> None:
    assert TITLES == [
        "README", "Posts", "Facebook", "Instagram", "Threads", "YouTube",
        "TikTok", "LinkedIn", "X", "Reddit", "Calendar", "_lists",
    ]  # fmt: skip


# --- a blank Sheet ---------------------------------------------------------------------------


def test_a_blank_sheet_becomes_the_full_layout() -> None:
    fake = FakeSheets()  # one empty default tab
    report = run(fake)

    assert fake.titles() == TITLES  # the blank default tab was reused, not left behind
    assert report.renamed == [("Sheet1", "README")] and report.left_alone == []
    assert fake.timezone == "Europe/Berlin"
    for spec in TABS[1:]:
        assert fake.header(spec.title) == [c.name for c in spec.columns], spec.title
    readme = fake._by_title("README")["values"]
    assert readme[0] == ["DK Publishing: how to use this Sheet"] and len(readme) > 10
    assert report.readme_written and not report.problems


def test_platform_tabs_get_the_plans_columns() -> None:
    fake = FakeSheets()
    run(fake)
    assert fake.header("Instagram") == [
        "post_key", "enabled", "account", "caption", "slot",
        "format", "share_to_feed", "cover_at_s",
        "status", "live_url", "last_error", "synced_at",
    ]  # fmt: skip
    assert fake.header("YouTube")[5:11] == [
        "title", "description", "tags", "category", "visibility_after_publish", "made_for_kids",
    ]  # fmt: skip
    assert "privacy_level" in fake.header("TikTok") and "subreddit" in fake.header("Reddit")


def test_a_dry_run_writes_nothing_but_reports_everything() -> None:
    fake = FakeSheets()
    report = run(fake, dry_run=True)
    assert fake.requests == [] and fake.value_writes == []
    assert fake.titles() == ["Sheet1"] and fake.timezone == "Etc/GMT"
    assert report.dry_run and report.timezone_set == "Europe/Berlin"
    assert report.renamed == [("Sheet1", "README")] and len(report.created) == len(TITLES) - 1
    assert report.readme_written


def test_running_it_twice_changes_nothing_the_second_time() -> None:
    fake = FakeSheets()
    run(fake)
    snapshot = (
        [list(map(list, t["values"])) for t in fake.tabs],
        [len(t["rules"]) for t in fake.tabs],
        [len(t["protected"]) for t in fake.tabs],
        fake.titles(),
    )
    first_requests = len(fake.requests)

    report = run(fake)

    assert not report.structural_changes and not report.problems
    again = (
        [list(map(list, t["values"])) for t in fake.tabs],
        [len(t["rules"]) for t in fake.tabs],
        [len(t["protected"]) for t in fake.tabs],
        fake.titles(),
    )
    assert again == snapshot  # no duplicated rules or protections, no moved tabs, no new rows
    second = fake.requests[first_requests:]
    kinds = {next(iter(r)) for r in second}
    assert "addSheet" not in kinds and "addConditionalFormatRule" not in kinds
    assert "addProtectedRange" not in kinds and "updateSpreadsheetProperties" not in kinds


# --- a Sheet people already use --------------------------------------------------------------


def test_existing_data_and_column_order_survive() -> None:
    posts = [["title", "post_key", "my_notes"], ["Eid platter", "DK-1", "keep me"]]
    fake = FakeSheets({"Posts": posts, "Sheet1": [["a budget I made"]]})
    report = run(fake)

    header = fake.header("Posts")
    assert header[:3] == ["title", "post_key", "my_notes"]  # order and extras kept
    assert header[3:] == [c for c in [c.name for c in tab("Posts").columns] if c not in header[:3]]
    assert fake._by_title("Posts")["values"][1] == ["Eid platter", "DK-1", "keep me"]
    assert fake._by_title("Sheet1")["values"] == [["a budget I made"]]
    assert "Sheet1" in report.left_alone and "README" in fake.titles()

    # post_key sits in column B here, and every rule that depends on it follows it there.
    rule = fake.validation("Posts", 1)
    assert rule and rule["condition"]["type"] == "CUSTOM_FORMULA"
    assert "$B$2:$B$1000" in rule["condition"]["values"][0]["userEnteredValue"]
    platform_rule = fake.validation("Instagram", 0)
    assert platform_rule and "'Posts'!$B$2:$B$1000" in str(platform_rule["condition"]["values"])


def test_a_readme_someone_wrote_is_never_overwritten() -> None:
    fake = FakeSheets({"README": [["my own notes"]]})
    report = run(fake)
    assert fake._by_title("README")["values"] == [["my own notes"]] and not report.readme_written


def test_a_header_with_a_repeated_name_is_reported_and_left_untouched() -> None:
    fake = FakeSheets({"Posts": [["post_key", "title", "title"]], "Sheet1": []})
    report = run(fake)
    assert any("'Posts'" in p and "same name twice" in p for p in report.problems)
    assert fake.header("Posts") == ["post_key", "title", "title"]
    assert "Posts" not in report.formatted and "Instagram" in report.formatted


def test_a_correct_time_zone_is_left_alone() -> None:
    fake = FakeSheets(timezone="Europe/Berlin")
    report = run(fake)
    assert report.timezone_set is None
    assert not any("updateSpreadsheetProperties" in r for r in fake.requests)


# --- the shape of what is written ------------------------------------------------------------


@pytest.fixture(scope="module")
def built() -> FakeSheets:
    fake = FakeSheets()
    run(fake)
    return fake


def col(title: str, name: str) -> int:
    return [c.name for c in tab(title).columns].index(name)


def test_dropdowns_offer_exactly_the_configured_choices(built: FakeSheets) -> None:
    rule = built.validation("Instagram", col("Instagram", "format"))
    assert rule and rule["strict"] is True
    assert rule["condition"]["type"] == "ONE_OF_LIST"
    assert [v["userEnteredValue"] for v in rule["condition"]["values"]] == [
        "feed",
        "carousel",
        "reel",
    ]


def test_checkboxes_and_slots_are_validated(built: FakeSheets) -> None:
    box = built.validation("Instagram", col("Instagram", "enabled"))
    assert box and box["condition"]["type"] == "BOOLEAN" and box["showCustomUi"] is True
    slot = built.validation("Instagram", col("Instagram", "slot"))
    assert slot and slot["condition"]["type"] == "DATE_IS_VALID" and slot["strict"] is True
    cover = built.validation("Instagram", col("Instagram", "cover_at_s"))
    assert cover and cover["condition"]["type"] == "NUMBER_GREATER_THAN_EQ"


def test_the_account_dropdown_reads_that_platforms_column_on_the_lists_tab(
    built: FakeSheets,
) -> None:
    lists_header = built.header("_lists")
    letter = col_letter(lists_header.index("accounts_instagram"))
    rule = built.validation("Instagram", col("Instagram", "account"))
    assert rule and rule["condition"]["type"] == "ONE_OF_RANGE" and rule["strict"] is False
    assert (
        rule["condition"]["values"][0]["userEnteredValue"]
        == f"='_lists'!${letter}$2:${letter}$1000"
    )
    other = built.validation("TikTok", col("TikTok", "account"))
    assert other and other["condition"]["values"] != rule["condition"]["values"]


def test_a_new_post_key_must_be_unique_and_a_platform_row_must_use_an_existing_one(
    built: FakeSheets,
) -> None:
    unique = built.validation("Posts", 0)
    assert (
        unique
        and "COUNTIF($A$2:$A$1000,A2)=1" in unique["condition"]["values"][0]["userEnteredValue"]
    )
    existing = built.validation("Facebook", 0)
    assert existing and existing["strict"] is True
    assert existing["condition"]["values"][0]["userEnteredValue"] == "='Posts'!$A$2:$A$1000"


def test_system_columns_have_no_dropdowns_but_are_grey_and_protected(built: FakeSheets) -> None:
    status = col("Instagram", "status")
    assert built.validation("Instagram", status) is None
    instagram_id = built._by_title("Instagram")["id"]
    protected = built._by_title("Instagram")["protected"]
    for name in ("status", "live_url", "last_error", "synced_at"):
        assert f"dk-publishing:system:Instagram:{name}" in protected
    assert "dk-publishing:header:Instagram" in protected
    assert not any("system:Instagram:caption" in p for p in protected)  # people's columns stay open
    assert all(
        r["protectedRange"]["warningOnly"] is True
        for r in built.of_kind("addProtectedRange")
        for r in [r]
    )
    grey = [
        b for b in built.of_kind("repeatCell")
        if b["range"]["sheetId"] == instagram_id
        and b["range"]["startColumnIndex"] == status
        and b["range"]["startRowIndex"] == 1
    ]  # fmt: skip
    assert grey and "backgroundColor" in grey[0]["cell"]["userEnteredFormat"]


def test_slots_display_as_german_date_times(built: FakeSheets) -> None:
    sid, slot = built._by_title("Facebook")["id"], col("Facebook", "slot")
    fmt = [
        b["cell"]["userEnteredFormat"]["numberFormat"]
        for b in built.of_kind("repeatCell")
        if b["range"]["sheetId"] == sid
        and b["range"]["startColumnIndex"] == slot
        and "numberFormat" in b["cell"]["userEnteredFormat"]
    ]  # fmt: skip
    assert fmt == [{"type": "DATE_TIME", "pattern": "dd.MM.yyyy HH:mm"}]


def test_every_status_column_gets_every_colour_rule_once(built: FakeSheets) -> None:
    rules = [t["rules"] for t in built.tabs]
    assert sum(len(r) for r in rules) == STATUS_COLUMNS * len(sheet_init.STATUS_RULES)
    published = [
        r for t in built.tabs for r in t["rules"]
        if r["booleanRule"]["condition"]["values"][0]["userEnteredValue"] == "published"
    ]  # fmt: skip
    assert len(published) == STATUS_COLUMNS


def test_headers_carry_help_notes_and_the_first_row_is_frozen(built: FakeSheets) -> None:
    sid, ci = built._by_title("YouTube")["id"], col("YouTube", "made_for_kids")
    notes = [
        b["rows"][0]["values"][0]["note"]
        for b in built.of_kind("updateCells")
        if b["start"] == {"sheetId": sid, "rowIndex": 0, "columnIndex": ci}
    ]  # fmt: skip
    assert notes and notes[0].startswith("Required.")
    frozen = [
        b for b in built.of_kind("updateSheetProperties")
        if b["properties"].get("gridProperties", {}).get("frozenRowCount") == 1
    ]  # fmt: skip
    assert len(frozen) == len(TITLES) - 1  # every tab except README


def test_the_tabs_end_up_in_the_plans_order_even_when_they_started_shuffled() -> None:
    fake = FakeSheets({"_lists": [], "Posts": [], "Instagram": []})
    run(fake)
    assert fake.titles() == TITLES
