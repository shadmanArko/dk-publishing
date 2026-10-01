"""Create or repair the Google Sheet from config/sheet.yaml, without ever overwriting data.

The rules that make it safe to re-run on a Sheet people are already using:
- existing tabs and rows are never cleared; only missing headers are appended on the right
  (the sync finds columns by header name, so position does not matter);
- an existing tab is never recreated, and unrelated tabs are left alone;
- formatting, validation and column widths are overwritten (they are ours); conditional-format
  rules and protected ranges are only added when absent, so a re-run never duplicates them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from dk_publishing.adapters.config.platforms import PlatformSettings
from dk_publishing.adapters.config.sheet_layout import Column, SheetLayout

README = "README"
POSTS = "Posts"
CALENDAR = "Calendar"
LISTS = "_lists"
PROTECT_TAG = "dk-publishing"
SCAN = "A1:AZ3"  # enough to tell whether a tab is empty and to read its header row
CHUNK = 150  # requests per batchUpdate

NAVY, GREY_HEAD, GREY_BODY, GREY_TEXT = "#1f2a44", "#5f6368", "#f1f3f4", "#5f6368"
STATUS_RULES: tuple[tuple[str, str, str, str], ...] = (
    # (text contained, background, text colour, meaning)
    ("published", "#d9ead3", "#274e13", "done"),
    ("live", "#d9ead3", "#274e13", "done"),
    ("failed", "#f4cccc", "#660000", "problem"),
    ("expired", "#f4cccc", "#660000", "problem"),
    ("invalid", "#fce5cd", "#783f04", "needs you"),
    ("unknown", "#fce5cd", "#783f04", "needs you"),
    ("reconnect", "#fce5cd", "#783f04", "needs you"),
    ("approved", "#cfe2f3", "#073763", "in progress"),
    ("preparing", "#cfe2f3", "#073763", "in progress"),
    ("prepared", "#cfe2f3", "#073763", "in progress"),
    ("publishing", "#cfe2f3", "#073763", "in progress"),
    ("scheduled", "#cfe2f3", "#073763", "in progress"),
    ("cancelled", "#eeeeee", "#444444", "stopped"),
    ("draft", "#eeeeee", "#444444", "stopped"),
)
WIDTHS = {
    "long_text": 360,
    "text": 170,
    "date_time": 150,
    "timestamp": 150,
    "checkbox": 85,
    "number": 95,
    "dropdown": 150,
    "list": 150,
    "account": 170,
    "post_key": 140,
    "unique_key": 140,
    "status": 140,
    "url": 220,
}


@dataclass(frozen=True, slots=True)
class TabSpec:
    title: str
    columns: tuple[Column, ...]
    platform: str | None = None


@dataclass
class TabState:
    sheet_id: int
    title: str
    index: int
    row_count: int
    col_count: int
    header: list[str]
    empty: bool
    rule_keys: set[tuple[int, str]] = field(default_factory=set)
    protected: set[str] = field(default_factory=set)


@dataclass
class SheetState:
    timezone: str
    tabs: dict[str, TabState]


@dataclass
class InitReport:
    dry_run: bool
    timezone_set: str | None = None
    renamed: list[tuple[str, str]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    headers_added: dict[str, list[str]] = field(default_factory=dict)
    formatted: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)
    readme_written: bool = False
    reordered: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def structural_changes(self) -> bool:
        return bool(
            self.timezone_set
            or self.renamed
            or self.created
            or self.headers_added
            or self.readme_written
            or self.reordered
        )


class HeaderError(Exception):
    """A tab's existing header row cannot be extended safely."""


# --- the tabs we want -------------------------------------------------------------------------


def build_tabs(layout: SheetLayout, platforms: Mapping[str, PlatformSettings]) -> list[TabSpec]:
    accounts = tuple(
        Column(
            name=f"accounts_{key}",
            kind="text",
            system=True,
            note="Connected accounts on this platform. Filled in by the system.",
        )
        for key in platforms
    )
    tabs = [
        TabSpec(README, ()),
        TabSpec(POSTS, layout.posts),
        *(TabSpec(s.tab, layout.platform_columns(key), key) for key, s in platforms.items()),
        TabSpec(CALENDAR, layout.calendar),
        TabSpec(LISTS, (*accounts, *layout.lists)),
    ]
    titles = [t.title for t in tabs]
    if len(set(titles)) != len(titles):
        raise ValueError(f"two tabs share a name: {titles}")
    return tabs


def merge_header(existing: Sequence[str], wanted: Sequence[str]) -> tuple[list[str], list[str]]:
    """(final header row, names appended). Existing cells keep their positions."""
    named = [h for h in existing if h]
    if len(named) != len(set(named)):
        raise HeaderError("the header row has the same name twice")
    added = [name for name in wanted if name not in existing]
    return [*existing, *added], added


# --- reading the Sheet ------------------------------------------------------------------------


def read_state(sheets: Any, sheet_id: str) -> SheetState:
    meta = (
        sheets.spreadsheets()
        .get(
            spreadsheetId=sheet_id,
            fields=(
                "properties.timeZone,sheets(properties(sheetId,title,index,"
                "gridProperties(rowCount,columnCount)),"
                "conditionalFormats,protectedRanges(description))"
            ),
        )
        .execute()
    )
    raw = meta.get("sheets", [])
    titles = [s["properties"]["title"] for s in raw]
    ranges = [f"'{t}'!{SCAN}" for t in titles]
    scanned = (
        sheets.spreadsheets()
        .values()
        .batchGet(spreadsheetId=sheet_id, ranges=ranges, valueRenderOption="FORMATTED_VALUE")
        .execute()
        if ranges
        else {"valueRanges": []}
    )
    tabs: dict[str, TabState] = {}
    for item, found in zip(raw, scanned["valueRanges"], strict=True):
        props, grid = item["properties"], item["properties"].get("gridProperties", {})
        values: list[list[Any]] = found.get("values", [])
        header = [str(c).strip() for c in (values[0] if values else [])]
        while header and not header[-1]:
            header.pop()
        tabs[props["title"]] = TabState(
            sheet_id=props["sheetId"],
            title=props["title"],
            index=props["index"],
            row_count=grid.get("rowCount", 0),
            col_count=grid.get("columnCount", 0),
            header=header,
            empty=not any(str(cell).strip() for row in values for cell in row),
            rule_keys={_rule_key(r) for r in item.get("conditionalFormats", [])} - {(-1, "")},
            protected={p.get("description", "") for p in item.get("protectedRanges", [])},
        )
    return SheetState(meta["properties"].get("timeZone", ""), tabs)


def _rule_key(rule: Mapping[str, Any]) -> tuple[int, str]:
    try:
        column = rule["ranges"][0]["startColumnIndex"]
        text = rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
    except (KeyError, IndexError):
        return (-1, "")
    return (column, str(text).lower())


# --- orchestration ----------------------------------------------------------------------------


def initialise(
    sheets: Any,
    sheet_id: str,
    layout: SheetLayout,
    platforms: Mapping[str, PlatformSettings],
    *,
    dry_run: bool,
) -> InitReport:
    tabs = build_tabs(layout, platforms)
    titles = [t.title for t in tabs]
    state = read_state(sheets, sheet_id)
    report = InitReport(dry_run=dry_run)
    structural: list[dict[str, Any]] = []

    if state.timezone != layout.timezone:
        report.timezone_set = layout.timezone
        structural.append(
            {
                "updateSpreadsheetProperties": {
                    "properties": {"timeZone": layout.timezone},
                    "fields": "timeZone",
                }
            }
        )

    if README not in state.tabs:  # reuse the blank default tab instead of leaving it behind
        blanks = sorted(
            (t for t in state.tabs.values() if t.title not in titles and t.empty),
            key=lambda t: t.index,
        )
        if blanks:
            old = blanks[0]
            report.renamed.append((old.title, README))
            structural.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": old.sheet_id, "title": README},
                        "fields": "title",
                    }
                }
            )
            state.tabs[README] = state.tabs.pop(old.title)
            state.tabs[README].title = README
    report.left_alone = [t for t in state.tabs if t not in titles]

    finals: dict[str, list[str]] = {}
    skipped: set[str] = set()
    for position, spec in enumerate(tabs):
        existing = state.tabs.get(spec.title)
        header = existing.header if existing else []
        try:
            final, added = merge_header(header, [c.name for c in spec.columns])
        except HeaderError as exc:
            report.problems.append(
                f"Tab {spec.title!r}: {exc}. Fix it by hand; the tab was skipped."
            )
            skipped.add(spec.title)
            finals[spec.title] = header
            continue
        finals[spec.title] = final
        if added:
            report.headers_added[spec.title] = added
        want_cols = max(len(final) + 1, 8 if spec.columns else 2)
        if existing is None:
            report.created.append(spec.title)
            structural.append(
                {
                    "addSheet": {
                        "properties": {
                            "title": spec.title,
                            "index": position,
                            "gridProperties": {"rowCount": layout.rows, "columnCount": want_cols},
                        }
                    }
                }
            )
        elif existing.row_count < layout.rows or existing.col_count < want_cols:
            structural.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": existing.sheet_id,
                            "gridProperties": {
                                "rowCount": max(existing.row_count, layout.rows),
                                "columnCount": max(existing.col_count, want_cols),
                            },
                        },
                        "fields": "gridProperties.rowCount,gridProperties.columnCount",
                    }
                }
            )

    managed = [t for t in tabs if t.title not in skipped]
    report.formatted = [t.title for t in managed]
    existing_order = [
        t.title for t in sorted(state.tabs.values(), key=lambda t: t.index) if t.title in titles
    ]
    report.reordered = any(t in state.tabs for t in titles) and existing_order != [
        t for t in titles if t in state.tabs
    ]
    if dry_run:
        if README in report.created or (README in state.tabs and state.tabs[README].empty):
            report.readme_written = True
        return report

    for batch in _chunks(structural):
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": batch}
        ).execute()

    state = read_state(sheets, sheet_id)  # fresh sheet ids and grid sizes
    _write_headers(sheets, sheet_id, state, finals, report)
    _write_readme(sheets, sheet_id, state, layout, report)

    letters = _letters(finals)
    requests: list[dict[str, Any]] = []
    for spec in managed:
        requests.extend(
            _format_tab(spec, state.tabs[spec.title], finals[spec.title], layout, letters)
        )
    requests.extend(_order_requests(state, titles))
    for batch in _chunks(requests):
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": batch}
        ).execute()
    return report


def _chunks(requests: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [requests[i : i + CHUNK] for i in range(0, len(requests), CHUNK)]


def _write_headers(
    sheets: Any,
    sheet_id: str,
    state: SheetState,
    finals: Mapping[str, list[str]],
    report: InitReport,
) -> None:
    data = []
    for title, added in report.headers_added.items():
        start = len(finals[title]) - len(added)
        data.append(
            {
                "range": f"'{title}'!{col_letter(start)}1:{col_letter(len(finals[title]) - 1)}1",
                "values": [added],
            }
        )
    if data:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": data}
        ).execute()


def _write_readme(
    sheets: Any, sheet_id: str, state: SheetState, layout: SheetLayout, report: InitReport
) -> None:
    tab = state.tabs[README]
    if not tab.empty or not layout.readme:
        return  # never overwrite a README someone has written
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{README}'!A1",
        valueInputOption="RAW",
        body={"values": [[line] for line in layout.readme]},
    ).execute()
    report.readme_written = True


def _order_requests(state: SheetState, titles: Sequence[str]) -> list[dict[str, Any]]:
    """Restate the whole order when it is wrong.

    Moving one tab shifts the others, so fixing only the tabs that look out of place can leave
    the result wrong. Setting every managed tab's position, first to last, always converges.
    """
    present = [t for t in titles if t in state.tabs]
    current = [
        t.title for t in sorted(state.tabs.values(), key=lambda t: t.index) if t.title in present
    ]
    if current == present:
        return []
    return [
        {
            "updateSheetProperties": {
                "properties": {"sheetId": state.tabs[title].sheet_id, "index": position},
                "fields": "index",
            }
        }
        for position, title in enumerate(present)
    ]


# --- request builders (pure) ------------------------------------------------------------------


def col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _letters(finals: Mapping[str, list[str]]) -> dict[tuple[str, str], str]:
    return {
        (title, name): col_letter(i)
        for title, header in finals.items()
        for i, name in enumerate(header)
        if name
    }


def _rgb(hex_colour: str) -> dict[str, float]:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def _grid(sheet_id: int, r0: int, r1: int | None, c0: int, c1: int) -> dict[str, int]:
    out = {"sheetId": sheet_id, "startRowIndex": r0, "startColumnIndex": c0, "endColumnIndex": c1}
    if r1 is not None:
        out["endRowIndex"] = r1
    return out


def _format_tab(
    spec: TabSpec,
    tab: TabState,
    header: list[str],
    layout: SheetLayout,
    letters: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    sid, rows = tab.sheet_id, layout.rows
    if spec.title == README:
        return _format_readme(sid)

    reqs: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {
                        "frozenRowCount": 1,
                        "frozenColumnCount": 1 if spec.title not in (CALENDAR, LISTS) else 0,
                    },
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }
    ]
    protect_header = f"{PROTECT_TAG}:header:{spec.title}"
    if protect_header not in tab.protected:
        reqs.append(_protect(_grid(sid, 0, 1, 0, max(len(header), 1)), protect_header))

    for column in spec.columns:
        ci = header.index(column.name)
        reqs.extend(_format_column(spec, tab, column, ci, layout, letters))
        if column.kind == "status":
            reqs.extend(_status_rules(sid, ci, rows, tab.rule_keys))
        if column.system:
            description = f"{PROTECT_TAG}:system:{spec.title}:{column.name}"
            if description not in tab.protected:
                reqs.append(_protect(_grid(sid, 1, rows, ci, ci + 1), description))
    return reqs


def _protect(grid: dict[str, int], description: str) -> dict[str, Any]:
    # warningOnly: people get a "are you sure" prompt rather than a hard block (the service
    # account, which writes these columns, is not interrupted).
    return {
        "addProtectedRange": {
            "protectedRange": {"range": grid, "description": description, "warningOnly": True}
        }
    }


def _format_readme(sid: int) -> list[dict[str, Any]]:
    return [
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 900},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": _grid(sid, 0, None, 0, 1),
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
            }
        },
        {
            "repeatCell": {
                "range": _grid(sid, 0, 1, 0, 1),
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                "fields": "userEnteredFormat.textFormat(bold,fontSize)",
            }
        },
    ]


def _format_column(
    spec: TabSpec,
    tab: TabState,
    column: Column,
    ci: int,
    layout: SheetLayout,
    letters: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    sid, rows = tab.sheet_id, layout.rows
    head_bg = GREY_HEAD if column.system else NAVY
    note = ("Required. " if column.required else "") + column.note
    reqs: list[dict[str, Any]] = [
        {
            "repeatCell": {
                "range": _grid(sid, 0, 1, ci, ci + 1),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb(head_bg),
                        "textFormat": {"bold": True, "foregroundColor": _rgb("#ffffff")},
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"
                ),
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": ci,
                    "endIndex": ci + 1,
                },
                "properties": {"pixelSize": WIDTHS.get(column.kind, 150)},
                "fields": "pixelSize",
            }
        },
    ]
    if note:
        reqs.append(
            {
                "updateCells": {
                    "rows": [{"values": [{"note": note.strip()}]}],
                    "fields": "note",
                    "start": {"sheetId": sid, "rowIndex": 0, "columnIndex": ci},
                }
            }
        )

    body: dict[str, Any] = {}
    if column.system:
        body["backgroundColor"] = _rgb(GREY_BODY)
        body["textFormat"] = {"foregroundColor": _rgb(GREY_TEXT)}
    if column.kind == "long_text":
        body["wrapStrategy"] = "WRAP"
        body["verticalAlignment"] = "TOP"
    if column.kind in ("date_time", "timestamp"):
        body["numberFormat"] = {"type": "DATE_TIME", "pattern": layout.slot_format}
    if body:
        reqs.append(
            {
                "repeatCell": {
                    "range": _grid(sid, 1, rows, ci, ci + 1),
                    "cell": {"userEnteredFormat": body},
                    "fields": "userEnteredFormat(" + ",".join(body) + ")",
                }
            }
        )

    rule = None if column.system else _validation(spec, column, ci, rows, letters)
    if rule is not None:
        reqs.append({"setDataValidation": {"range": _grid(sid, 1, rows, ci, ci + 1), "rule": rule}})
    return reqs


def _range_ref(title: str, letter: str, rows: int) -> str:
    return f"='{title}'!${letter}$2:${letter}${rows}"


def _validation(
    spec: TabSpec, column: Column, ci: int, rows: int, letters: Mapping[tuple[str, str], str]
) -> dict[str, Any] | None:
    kind = column.kind
    if kind == "checkbox":
        return {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}
    if kind == "date_time":
        return {
            "condition": {"type": "DATE_IS_VALID"},
            "strict": True,
            "inputMessage": "A real date and time (Berlin), e.g. 14.11.2026 18:00. Not text.",
        }
    if kind == "number":
        return {
            "condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "0"}]},
            "strict": True,
        }
    if kind == "dropdown":
        values = [{"userEnteredValue": option} for option in column.options]
        return {
            "condition": {"type": "ONE_OF_LIST", "values": values},
            "strict": True,
            "showCustomUi": True,
        }
    if kind in ("list", "account"):
        name = column.list if kind == "list" else f"accounts_{spec.platform}"
        letter = letters.get((LISTS, str(name)))
        if letter is None:
            return None
        # Not strict: the list is filled by people or by the system and may lag behind.
        return {
            "condition": {
                "type": "ONE_OF_RANGE",
                "values": [{"userEnteredValue": _range_ref(LISTS, letter, rows)}],
            },
            "strict": False,
            "showCustomUi": True,
        }
    if kind == "post_key":
        letter = letters.get((POSTS, column.name))
        if letter is None:
            return None
        return {
            "condition": {
                "type": "ONE_OF_RANGE",
                "values": [{"userEnteredValue": _range_ref(POSTS, letter, rows)}],
            },
            "strict": True,
            "showCustomUi": True,
            "inputMessage": "Pick a key that exists on the Posts tab.",
        }
    if kind == "unique_key":
        letter = col_letter(ci)
        formula = f"=COUNTIF(${letter}$2:${letter}${rows},{letter}2)=1"
        return {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
            "strict": True,
            "inputMessage": "Must be unique; never reuse or rename a key.",
        }
    return None


def _status_rules(
    sid: int, ci: int, rows: int, existing: set[tuple[int, str]]
) -> list[dict[str, Any]]:
    out = []
    for text, background, foreground, _ in STATUS_RULES:
        if (ci, text) in existing:
            continue
        out.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [_grid(sid, 1, rows, ci, ci + 1)],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_CONTAINS",
                                "values": [{"userEnteredValue": text}],
                            },
                            "format": {
                                "backgroundColor": _rgb(background),
                                "textFormat": {"foregroundColor": _rgb(foreground)},
                            },
                        },
                    },
                    "index": 0,
                }
            }
        )
    return out
