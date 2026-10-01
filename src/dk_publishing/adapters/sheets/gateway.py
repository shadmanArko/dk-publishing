"""Read the Sheet into typed rows and write status back, over the Google Sheets API.

Columns are found by header name, never by position, so people may reorder them. A tab whose
header is wrong is reported and skipped; it never stops the other tabs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from dk_publishing.adapters.config.platforms import PlatformSettings
from dk_publishing.adapters.config.sheet_layout import Column, SheetLayout
from dk_publishing.adapters.sheets.sheet_init import CALENDAR, LISTS, POSTS, col_letter
from dk_publishing.domain.publishing import Violation
from dk_publishing.domain.sheet import (
    PlatformRow,
    PostRow,
    RawRow,
    SheetSnapshot,
    StatusPlan,
)
from dk_publishing.domain.sync import parse_media
from dk_publishing.domain.timezones import local_to_serial, serial_to_local, utc_to_berlin

COMMON = {"post_key", "enabled", "account", "caption", "slot"}
SPAN = "AZ"  # columns read per tab


class SheetReadError(Exception):
    """Google refused or the response was not what a Sheet should look like."""


class GoogleSheetGateway:
    def __init__(
        self,
        sheets: Any,
        *,
        sheet_id: str,
        layout: SheetLayout,
        platforms: Mapping[str, PlatformSettings],
    ) -> None:
        self._sheets = sheets
        self._sheet_id = sheet_id
        self._layout = layout
        self._platforms = platforms
        self._tab_platform = {settings.tab: key for key, settings in platforms.items()}
        self._headers: dict[str, dict[str, int]] = {}
        self._existing: dict[str, list[list[Any]]] = {}
        self._row_counts: dict[str, int] = {}
        self._read_done = False

    # --- reading ---------------------------------------------------------------------------

    def read(self) -> SheetSnapshot:
        rows = self._layout.rows
        tabs = [POSTS, *self._tab_platform, CALENDAR, LISTS]
        found = (
            self._sheets.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=self._sheet_id,
                ranges=[f"'{t}'!A1:{SPAN}{rows}" for t in tabs],
                valueRenderOption="UNFORMATTED_VALUE",
                dateTimeRenderOption="SERIAL_NUMBER",
            )
            .execute()
        )
        grids = {t: v.get("values", []) for t, v in zip(tabs, found["valueRanges"], strict=True)}
        self._existing = {t: grids[t] for t in (CALENDAR, LISTS)}
        self._row_counts = {t: _last_used_row(g) for t, g in grids.items()}

        snapshot = SheetSnapshot()
        self._headers = {}
        for tab in tabs:
            grid = grids[tab]
            header = [str(c).strip() for c in (grid[0] if grid else [])]
            names = [h for h in header if h]
            if len(names) != len(set(names)):
                snapshot.problems.append(f"Tab '{tab}' has a repeated column name; it was skipped.")
                continue
            self._headers[tab] = {name: i for i, name in enumerate(header) if name}

        wanted: dict[str, Sequence[Column]] = {POSTS: self._layout.posts}
        for tab, key in self._tab_platform.items():
            wanted[tab] = self._layout.platform_columns(key)
        for tab, columns in wanted.items():
            if tab not in self._headers:
                continue
            missing = [c.name for c in columns if c.name not in self._headers[tab]]
            if missing:
                snapshot.problems.append(
                    f"Tab '{tab}' is missing column(s) {', '.join(missing)}. "
                    "Run `dk sheet init` to add them. The tab was skipped."
                )
                del self._headers[tab]
                continue
            self._parse_tab(tab, columns, grids[tab], snapshot)
        self._read_done = True
        return snapshot

    def _parse_tab(
        self, tab: str, columns: Sequence[Column], grid: list[list[Any]], out: SheetSnapshot
    ) -> None:
        index = self._headers[tab]
        for offset, values in enumerate(grid[1:], start=2):
            cells = {name: _at(values, i) for name, i in index.items()}
            user = [c for c in columns if not c.system]
            if not any(_present(cells[c.name]) for c in user):
                continue  # an empty row, or one with only unticked boxes
            out.raw.append(RawRow(tab, offset, _json_safe(cells)))
            problems: list[Violation] = []
            typed = {c.name: _coerce(c, cells[c.name], problems) for c in user}
            system = {c.name: cells[c.name] for c in columns if c.system}
            if tab == POSTS:
                out.posts.append(self._post(offset, typed, problems, system))
            else:
                out.rows.append(self._platform_row(tab, offset, columns, typed, problems, system))

    @staticmethod
    def _post(
        row: int, t: Mapping[str, Any], problems: list[Violation], system: Mapping[str, Any]
    ) -> PostRow:
        return PostRow(
            row=row,
            post_key=t["post_key"] or "",
            title=t["title"] or "",
            media=parse_media(t["media"] or ""),
            default_caption=t["default_caption"] or "",
            default_slot=t["default_slot"],
            ready=bool(t["ready"]),
            problems=tuple(problems),
            system=system,
        )

    def _platform_row(
        self,
        tab: str,
        row: int,
        columns: Sequence[Column],
        t: Mapping[str, Any],
        problems: list[Violation],
        system: Mapping[str, Any],
    ) -> PlatformRow:
        options = {
            c.name: t[c.name]
            for c in columns
            if not c.system and c.name not in COMMON and t[c.name] is not None
        }
        return PlatformRow(
            tab=tab,
            platform=self._tab_platform[tab],
            row=row,
            post_key=t["post_key"] or "",
            enabled=bool(t["enabled"]),
            account=t["account"] or "",
            caption=t["caption"] or "",
            slot=t["slot"],
            options=options,
            problems=tuple(problems),
            system=system,
        )

    # --- adding rows -----------------------------------------------------------------------

    def append_row(self, tab: str, values: Mapping[str, Any]) -> int:
        """Write one new row below the last used row of `tab`. Never overwrites a row.
        Values are written as given (booleans, numbers, serial dates, text)."""
        if not self._read_done or tab not in self._headers:
            raise RuntimeError(f"read() first, and the tab {tab!r} must be readable")
        row = self._row_counts[tab] + 1
        data = [
            {"range": f"'{tab}'!{col_letter(self._headers[tab][name])}{row}", "values": [[value]]}
            for name, value in values.items()
            if name in self._headers[tab]
        ]
        self._sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=self._sheet_id, body={"valueInputOption": "RAW", "data": data}
        ).execute()
        self._row_counts[tab] = row
        return row

    # --- writing ---------------------------------------------------------------------------

    def write(self, plan: StatusPlan) -> int:
        if not self._read_done:
            raise RuntimeError("read() must be called before write()")
        data: list[dict[str, Any]] = []
        clears: list[str] = []

        for r in plan.rows:
            stamp = _serial(r.synced_at)
            self._cells(data, r.tab, r.row, status=r.status, live_url=r.live_url,
                        last_error=r.last_error, synced_at=stamp)  # fmt: skip
        for p in plan.posts:
            self._cells(data, POSTS, p.row, status=p.status, last_error=p.last_error)

        if plan.calendar is not None:
            matrix = self._calendar_matrix(plan)
            if _differs(matrix, self._existing.get(CALENDAR, [])[1:]):
                clears.append(f"'{CALENDAR}'!A2:{SPAN}{self._layout.rows}")
                if matrix:
                    data.append({"range": f"'{CALENDAR}'!A2", "values": matrix})
        if plan.accounts is not None:
            self._account_lists(plan.accounts, data, clears)

        if clears:
            self._sheets.spreadsheets().values().batchClear(
                spreadsheetId=self._sheet_id, body={"ranges": clears}
            ).execute()
        if data:
            self._sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=self._sheet_id, body={"valueInputOption": "RAW", "data": data}
            ).execute()
        return sum(len(d["values"]) * len(d["values"][0]) for d in data)

    def _cells(self, data: list[dict[str, Any]], tab: str, row: int, **values: Any) -> None:
        index = self._headers.get(tab)
        if index is None:
            return
        for name, value in values.items():
            if name in index:
                data.append(
                    {"range": f"'{tab}'!{col_letter(index[name])}{row}", "values": [[value]]}
                )

    def _calendar_matrix(self, plan: StatusPlan) -> list[list[Any]]:
        index = self._headers.get(CALENDAR)
        if not index:
            return []
        width = max(index.values()) + 1
        matrix = []
        for e in plan.calendar or ():
            line: list[Any] = [""] * width
            for name, value in (
                ("slot", _serial(e.slot)),
                ("platform", e.platform),
                ("account", e.account),
                ("post_key", e.post_key),
                ("title", e.title),
                ("status", e.status),
                ("source", e.source),
            ):
                if name in index:
                    line[index[name]] = value
            matrix.append(line)
        return matrix

    def _account_lists(
        self, accounts: Mapping[str, Sequence[str]], data: list[dict[str, Any]], clears: list[str]
    ) -> None:
        index = self._headers.get(LISTS) or {}
        grid = self._existing.get(LISTS, [])
        for platform, names in accounts.items():
            column = f"accounts_{platform}"
            if column not in index:
                continue
            current = [_at(row, index[column]) for row in grid[1:]]
            if not _differs([[n] for n in names], [[c] for c in current]):
                continue
            letter = col_letter(index[column])
            clears.append(f"'{LISTS}'!{letter}2:{letter}{self._layout.rows}")
            if names:
                data.append({"range": f"'{LISTS}'!{letter}2", "values": [[n] for n in names]})


# --- cell helpers -----------------------------------------------------------------------------


def _at(values: Sequence[Any], i: int) -> Any:
    return values[i] if i < len(values) else None


def _last_used_row(grid: Sequence[Sequence[Any]]) -> int:
    """The last 1-based row that has something in it. Unticked checkboxes come back from the API
    as FALSE in every row of their column, so a row of only those counts as empty."""
    last = 1
    for number, values in enumerate(grid, start=1):
        if any(_present(cell) for cell in values):
            last = number
    return last


def _present(value: Any) -> bool:
    return value not in (None, "", False)


def _json_safe(cells: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cells.items() if v is not None}


def _serial(moment: datetime) -> float:
    return local_to_serial(utc_to_berlin(moment).replace(tzinfo=None))


def _norm(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _differs(new: Sequence[Sequence[Any]], old: Sequence[Sequence[Any]]) -> bool:
    """Compare as Sheets would show them, ignoring trailing empty rows and cells."""

    def canon(rows: Sequence[Sequence[Any]]) -> list[tuple[str, ...]]:
        out = [tuple(_norm(c) for c in row) for row in rows]
        out = [tuple(c for c in row) for row in out]
        trimmed = [_rstrip(row) for row in out]
        while trimmed and not trimmed[-1]:
            trimmed.pop()
        return trimmed

    return canon(new) != canon(old)


def _rstrip(row: tuple[str, ...]) -> tuple[str, ...]:
    end = len(row)
    while end and not row[end - 1]:
        end -= 1
    return row[:end]


def _coerce(column: Column, value: Any, problems: list[Violation]) -> Any:
    """Turn a raw cell into the column's type, recording a plain-language problem if it cannot."""
    blank = value is None or value == ""
    kind, name = column.kind, column.name
    if kind == "checkbox":
        if value in (True, "TRUE", "true", "True"):
            return True
        if blank or value in (False, "FALSE", "false", "False"):
            return False
        problems.append(Violation(name, f"{name} must be a tick box (ticked or not)."))
        return False
    if blank:
        if column.required:
            problems.append(Violation(name, f"{name} is required."))
        return None
    if kind == "date_time":
        if isinstance(value, bool) or not isinstance(value, int | float):
            problems.append(
                Violation(
                    name, f"{name} must be a real date and time (like 14.11.2026 18:00), not text."
                )
            )
            return None
        return serial_to_local(float(value))
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            problems.append(Violation(name, f"{name} must be a number."))
            return None
        return int(value) if float(value).is_integer() else float(value)
    text = _text(value)
    if kind == "dropdown" and text not in column.options:
        problems.append(Violation(name, f"{name} must be one of: {', '.join(column.options)}."))
    return text


def _text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
