from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from dk_publishing.adapters.config.platforms import load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.adapters.media.drive_catalog import DriveCatalog, sheet_modified_time
from dk_publishing.adapters.sheets.gateway import GoogleSheetGateway
from dk_publishing.adapters.sheets.sheet_init import build_tabs
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG, DEFAULT_SHEET_CONFIG
from dk_publishing.domain.sheet import CalendarEntry, PostStatus, RowStatus, StatusPlan
from dk_publishing.domain.timezones import local_to_serial

PLATFORMS = load_platforms(DEFAULT_PLATFORMS_CONFIG)
LAYOUT = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(PLATFORMS))
TABS = {t.title: [c.name for c in t.columns] for t in build_tabs(LAYOUT, PLATFORMS) if t.columns}
SLOT = local_to_serial(datetime(2026, 11, 14, 18, 0))


class Call:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


class FakeGoogle:
    """The values API, returning what a Sheet returns with UNFORMATTED_VALUE."""

    def __init__(self, grids: dict[str, list[list[Any]]]) -> None:
        self.grids = grids
        self.updates: list[dict[str, Any]] = []
        self.clears: list[str] = []
        self.render: dict[str, Any] = {}

    def spreadsheets(self) -> FakeGoogle:
        return self

    def values(self) -> FakeGoogle:
        return self

    def batchGet(self, *, spreadsheetId: str, ranges: list[str], **kw: Any) -> Call:
        self.render = kw
        out = []
        for rng in ranges:
            title = re.match(r"^'(.+)'!", rng)
            assert title
            out.append({"values": self.grids.get(title.group(1), [])})
        return Call({"valueRanges": out})

    def batchUpdate(self, *, spreadsheetId: str, body: dict[str, Any]) -> Call:
        assert body["valueInputOption"] == "RAW"  # never USER_ENTERED: no formula injection
        self.updates += body["data"]
        return Call({})

    def batchClear(self, *, spreadsheetId: str, body: dict[str, Any]) -> Call:
        self.clears += body["ranges"]
        return Call({})


def blank() -> dict[str, list[list[Any]]]:
    return {title: [list(header)] for title, header in TABS.items()}


def put(grids: dict[str, list[list[Any]]], tab: str, **cells: Any) -> int:
    header = grids[tab][0]
    grids[tab].append([cells.get(name) for name in header])
    return len(grids[tab])  # the 1-based row number just added


def gateway(grids: dict[str, list[list[Any]]]) -> tuple[GoogleSheetGateway, FakeGoogle]:
    fake = FakeGoogle(grids)
    return GoogleSheetGateway(fake, sheet_id="SID", layout=LAYOUT, platforms=PLATFORMS), fake


# --- reading ----------------------------------------------------------------------------------


def test_the_posts_tab_is_read_into_typed_rows() -> None:
    g = blank()
    row = put(
        g, "Posts", post_key="DK-1", title="Eid", media="a.mp4, b.jpg", default_caption="Hi",
        default_slot=SLOT, ready=True, status="2 of 3 live",
    )  # fmt: skip
    snapshot, fake = gateway(g)[0].read(), None
    [post] = snapshot.posts
    assert (post.row, post.post_key, post.title) == (row, "DK-1", "Eid")
    assert post.media == ("a.mp4", "b.jpg") and post.default_caption == "Hi"
    assert post.default_slot == datetime(2026, 11, 14, 18, 0) and post.ready is True
    assert post.system["status"] == "2 of 3 live" and not post.problems and fake is None


def test_a_platform_row_gets_its_typed_options() -> None:
    g = blank()
    put(
        g, "Instagram", post_key="DK-1", enabled=True, account="Main", caption="Own", slot=SLOT,
        format="reel", share_to_feed=True, cover_at_s=3.0, status="approved", last_error="",
    )  # fmt: skip
    [row] = (snapshot := gateway(g)[0].read()).rows
    assert (row.tab, row.platform, row.account, row.caption) == (
        "Instagram",
        "instagram",
        "Main",
        "Own",
    )
    assert row.enabled and row.slot == datetime(2026, 11, 14, 18, 0)
    assert row.options == {"format": "reel", "share_to_feed": True, "cover_at_s": 3}
    assert isinstance(row.options["cover_at_s"], int)  # 3.0 from the API becomes 3
    assert row.system["status"] == "approved" and not snapshot.problems


def test_it_asks_google_for_raw_values_and_serial_dates() -> None:
    gw, fake = gateway(blank())
    gw.read()
    assert fake.render == {
        "valueRenderOption": "UNFORMATTED_VALUE",
        "dateTimeRenderOption": "SERIAL_NUMBER",
    }


def test_empty_rows_and_rows_with_only_unticked_boxes_are_ignored() -> None:
    g = blank()
    put(g, "Posts")
    put(g, "Instagram", enabled=False, share_to_feed=False)
    real = put(g, "Posts", post_key="DK-1")
    snapshot = gateway(g)[0].read()
    assert [p.row for p in snapshot.posts] == [real] and snapshot.rows == []


def test_columns_are_found_by_name_so_reordering_is_safe() -> None:
    g = blank()
    g["Posts"][0].reverse()
    put(g, "Posts", post_key="DK-1", title="Eid", ready=True, default_slot=SLOT)
    [post] = gateway(g)[0].read().posts
    assert (post.post_key, post.title, post.ready, post.default_slot) == (
        "DK-1", "Eid", True, datetime(2026, 11, 14, 18, 0),
    )  # fmt: skip


def test_a_numeric_key_is_read_as_text() -> None:
    g = blank()
    put(g, "Posts", post_key=2026.0)
    assert gateway(g)[0].read().posts[0].post_key == "2026"


@pytest.mark.parametrize(
    ("cells", "field", "words"),
    [
        ({"slot": "14.11.2026 18:00"}, "slot", "real date and time"),
        ({"slot": True}, "slot", "real date and time"),
        ({"format": "story"}, "format", "must be one of: feed, carousel, reel"),
        ({"account": None}, "account", "account is required"),
        ({"format": None}, "format", "format is required"),
        ({"cover_at_s": "soon"}, "cover_at_s", "must be a number"),
        ({"enabled": "maybe"}, "enabled", "tick box"),
    ],
)
def test_cell_problems_are_explained_per_field(
    cells: dict[str, Any], field: str, words: str
) -> None:
    g = blank()
    base = dict(post_key="DK-1", enabled=True, account="Main", format="reel", slot=SLOT)
    put(g, "Instagram", **{**base, **cells})
    [row] = gateway(g)[0].read().rows
    assert any(p.field == field and words in p.message for p in row.problems), row.problems


def test_a_tab_with_a_missing_column_is_reported_and_skipped_but_others_still_read() -> None:
    g = blank()
    i = g["Instagram"][0].index("format")
    g["Instagram"][0].pop(i)
    put(g, "Posts", post_key="DK-1")
    snapshot = gateway(g)[0].read()
    assert len(snapshot.posts) == 1 and snapshot.rows == []
    [problem] = snapshot.problems
    assert "'Instagram'" in problem and "format" in problem and "dk sheet init" in problem


def test_a_repeated_header_is_reported_and_the_tab_skipped() -> None:
    g = blank()
    g["Posts"][0].append("title")
    put(g, "Posts", post_key="DK-1")
    snapshot = gateway(g)[0].read()
    assert snapshot.posts == [] and any("repeated column name" in p for p in snapshot.problems)


def test_extra_columns_people_added_are_ignored() -> None:
    g = blank()
    g["Posts"][0].append("my_notes")
    g["Posts"].append([None] * len(g["Posts"][0]))
    g["Posts"][1][0], g["Posts"][1][-1] = "DK-1", "private"
    [post] = gateway(g)[0].read().posts
    assert post.post_key == "DK-1"


def test_raw_rows_are_kept_for_the_audit_copy() -> None:
    g = blank()
    put(g, "Posts", post_key="DK-1", title="Eid")
    [raw] = gateway(g)[0].read().raw
    assert (raw.tab, raw.row, raw.cells["post_key"]) == ("Posts", 2, "DK-1")
    assert "default_caption" not in raw.cells  # empty cells are not stored


# --- writing ----------------------------------------------------------------------------------

NOW = datetime(2026, 11, 14, 11, 0, tzinfo=UTC)  # 12:00 in Berlin


def cell(fake: FakeGoogle, rng: str) -> Any:
    [match] = [u for u in fake.updates if u["range"] == rng]
    return match["values"][0][0]


def test_status_goes_into_the_right_cells_even_after_columns_were_reordered() -> None:
    g = blank()
    g["Instagram"][0].reverse()  # the last column (synced_at) is now first
    put(g, "Instagram", post_key="DK-1", account="Main", format="reel")
    gw, fake = gateway(g)
    gw.read()
    gw.write(StatusPlan(rows=[RowStatus("Instagram", 2, "approved", "https://x/1", "oops", NOW)]))

    def where(name: str) -> str:
        return f"'Instagram'!{chr(65 + g['Instagram'][0].index(name))}2"

    assert where("synced_at") == "'Instagram'!A2"
    assert cell(fake, where("status")) == "approved"
    assert (
        cell(fake, where("live_url")) == "https://x/1" and cell(fake, where("last_error")) == "oops"
    )
    assert cell(fake, where("synced_at")) == pytest.approx(
        local_to_serial(datetime(2026, 11, 14, 12, 0))  # stored as Berlin time, as a Sheets date
    )


def test_the_posts_roll_up_is_written() -> None:
    gw, fake = gateway(blank())
    gw.read()
    gw.write(StatusPlan(posts=[PostStatus(2, "1 of 2 live", "Instagram: oops")]))
    assert (
        cell(fake, "'Posts'!G2") == "1 of 2 live" and cell(fake, "'Posts'!H2") == "Instagram: oops"
    )


def test_an_error_that_looks_like_a_formula_is_stored_as_text() -> None:
    gw, fake = gateway(blank())
    gw.read()
    gw.write(StatusPlan(posts=[PostStatus(2, "", '=HYPERLINK("http://evil")')]))
    assert (
        cell(fake, "'Posts'!H2") == '=HYPERLINK("http://evil")'
    )  # RAW: Sheets will not evaluate it


def test_the_calendar_is_rewritten_only_when_it_differs() -> None:
    entry = CalendarEntry(NOW, "instagram", "Main", "DK-1", "Eid", "approved", "human")
    gw, fake = gateway(blank())
    gw.read()
    gw.write(StatusPlan(calendar=[entry]))
    assert fake.clears == ["'Calendar'!A2:AZ1000"]
    [block] = [u for u in fake.updates if u["range"] == "'Calendar'!A2"]
    assert block["values"][0][:2] == [
        pytest.approx(local_to_serial(datetime(2026, 11, 14, 12, 0))),
        "instagram",
    ]

    # the same calendar already in the Sheet: no clear, no write
    g = blank()
    g["Calendar"].append(block["values"][0])
    gw2, fake2 = gateway(g)
    gw2.read()
    assert (
        gw2.write(StatusPlan(calendar=[entry])) == 0 and fake2.clears == [] and fake2.updates == []
    )


def test_account_lists_are_written_per_platform_and_only_when_changed() -> None:
    gw, fake = gateway(blank())
    gw.read()
    gw.write(StatusPlan(accounts={"instagram": ["Main", "Second"], "facebook": []}))
    column = chr(65 + TABS["_lists"].index("accounts_instagram"))
    [block] = [u for u in fake.updates if u["range"] == f"'_lists'!{column}2"]
    assert block["values"] == [["Main"], ["Second"]]
    assert fake.clears == [f"'_lists'!{column}2:{column}1000"]

    g = blank()
    for name in ("Main", "Second"):
        g["_lists"].append([None] * len(TABS["_lists"]))
        g["_lists"][-1][TABS["_lists"].index("accounts_instagram")] = name
    gw2, fake2 = gateway(g)
    gw2.read()
    assert gw2.write(StatusPlan(accounts={"instagram": ["Main", "Second"]})) == 0
    assert fake2.updates == [] and fake2.clears == []


def test_writing_nothing_makes_no_calls() -> None:
    gw, fake = gateway(blank())
    gw.read()
    assert gw.write(StatusPlan()) == 0 and fake.updates == [] and fake.clears == []


def test_writing_before_reading_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError, match="read"):
        gateway(blank())[0].write(StatusPlan())


def test_a_new_row_goes_below_the_last_used_row_and_never_over_data() -> None:
    g = blank()
    put(g, "Posts", post_key="DK-1")
    put(g, "Posts", post_key="DK-2")
    gw, fake = gateway(g)
    gw.read()
    assert gw.append_row("Posts", {"post_key": "DK-3", "ready": False, "default_slot": SLOT}) == 4
    assert gw.append_row("Posts", {"post_key": "DK-4"}) == 5
    assert cell(fake, "'Posts'!A4") == "DK-3" and cell(fake, "'Posts'!F4") is False
    assert cell(fake, "'Posts'!A5") == "DK-4"


# --- Drive ------------------------------------------------------------------------------------


class DriveFiles:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def files(self) -> DriveFiles:
        return self

    def list(self, **kw: Any) -> Call:
        self.calls.append(kw)
        return Call(self.pages[len(self.calls) - 1])

    def get(self, **kw: Any) -> Call:
        return Call({"modifiedTime": "2026-10-01T10:00:00.123Z"})


def test_the_drive_catalog_follows_pages_and_skips_folders() -> None:
    drive = DriveFiles(
        [
            {"files": [{"id": "1", "name": "a.mp4", "md5Checksum": "m1", "size": "42"}], "nextPageToken": "t"},
            {"files": [
                {"id": "2", "name": "sub", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "3", "name": "b.jpg", "mimeType": "image/jpeg"},
            ]},
        ]
    )  # fmt: skip
    files = DriveCatalog(drive, "FOLDER").list_files()
    assert [(f.id, f.name, f.md5, f.size) for f in files] == [
        ("1", "a.mp4", "m1", 42),
        ("3", "b.jpg", None, None),
    ]
    assert drive.calls[1]["pageToken"] == "t" and "'FOLDER' in parents" in drive.calls[0]["q"]


def test_the_sheets_modified_time_is_returned_as_text() -> None:
    assert sheet_modified_time(DriveFiles([]), "SID") == "2026-10-01T10:00:00.123Z"
