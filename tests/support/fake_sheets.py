"""An in-memory stand-in for the slice of the Google Sheets API that `sheet init` uses.

It applies structural requests (tabs, grid size, order, rules, protected ranges, values) so a test
can run the initialiser twice and look at the resulting Sheet, and it logs every request so a test
can assert on exact shapes.
"""

from __future__ import annotations

import re
from typing import Any

_A1 = re.compile(r"^'(.+)'!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$")


class Call:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


def letters_to_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheets:
    def __init__(
        self, tabs: dict[str, list[list[str]]] | None = None, timezone: str = "Etc/GMT"
    ) -> None:
        self.timezone = timezone
        self.tabs: list[dict[str, Any]] = []
        self._next_id = 100
        self.requests: list[dict[str, Any]] = []  # structural and formatting, in order
        self.value_writes: list[tuple[str, list[list[str]]]] = []
        for title, values in (tabs if tabs is not None else {"Sheet1": []}).items():
            self._add(title, values, rows=1000, cols=26)

    # --- the googleapiclient-shaped surface ---
    def spreadsheets(self) -> FakeSheets:
        return self

    def values(self) -> FakeSheets:
        return self

    def get(self, *, spreadsheetId: str, fields: str) -> Call:
        return Call(
            {
                "properties": {"timeZone": self.timezone},
                "sheets": [
                    {
                        "properties": {
                            "sheetId": t["id"],
                            "title": t["title"],
                            "index": i,
                            "gridProperties": {"rowCount": t["rows"], "columnCount": t["cols"]},
                        },
                        "conditionalFormats": t["rules"],
                        "protectedRanges": [{"description": d} for d in t["protected"]],
                    }
                    for i, t in enumerate(self.tabs)
                ],
            }
        )

    def batchGet(self, *, spreadsheetId: str, ranges: list[str], **_: Any) -> Call:
        found = []
        for rng in ranges:
            title = re.match(r"^'(.+)'!", rng)
            assert title
            tab = self._by_title(title.group(1))
            rows = [self._trim(row) for row in tab["values"][:3]]
            found.append({"values": rows} if any(rows) else {})
        return Call({"valueRanges": found})

    def batchUpdate(self, *, spreadsheetId: str, body: dict[str, Any]) -> Call:
        if "requests" in body:
            return Call({"replies": [self._apply(r) for r in body["requests"]]})
        for item in body["data"]:  # values.batchUpdate
            self._write(item["range"], item["values"])
        return Call({})

    def update(self, *, spreadsheetId: str, range: str, body: dict[str, Any], **_: Any) -> Call:
        self._write(range, body["values"])
        return Call({})

    # --- simulation ---
    def _add(self, title: str, values: list[list[str]], rows: int, cols: int) -> dict[str, Any]:
        self._next_id += 1
        tab = {
            "id": self._next_id,
            "title": title,
            "rows": rows,
            "cols": cols,
            "values": [list(r) for r in values],
            "rules": [],
            "protected": [],
        }
        self.tabs.append(tab)
        return tab

    def _by_title(self, title: str) -> dict[str, Any]:
        return next(t for t in self.tabs if t["title"] == title)

    def _by_id(self, sheet_id: int) -> dict[str, Any]:
        return next(t for t in self.tabs if t["id"] == sheet_id)

    @staticmethod
    def _trim(row: list[str]) -> list[str]:
        row = list(row)
        while row and not row[-1]:
            row.pop()
        return row

    def _write(self, rng: str, values: list[list[str]]) -> None:
        match = _A1.match(rng)
        assert match, rng
        tab = self._by_title(match.group(1))
        col, row = letters_to_index(match.group(2)), int(match.group(3)) - 1
        self.value_writes.append((rng, values))
        for r, line in enumerate(values):
            while len(tab["values"]) <= row + r:
                tab["values"].append([])
            target = tab["values"][row + r]
            while len(target) < col + len(line):
                target.append("")
            target[col : col + len(line)] = line

    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        ((kind, body),) = request.items()
        if kind == "updateSpreadsheetProperties":
            self.timezone = body["properties"]["timeZone"]
        elif kind == "addSheet":
            props = body["properties"]
            grid = props["gridProperties"]
            tab = self._add(props["title"], [], grid["rowCount"], grid["columnCount"])
            self.tabs.insert(props["index"], self.tabs.pop())
            return {"addSheet": {"properties": {"sheetId": tab["id"]}}}
        elif kind == "updateSheetProperties":
            props, fields = body["properties"], body["fields"]
            tab = self._by_id(props["sheetId"])
            if "title" in fields:
                tab["title"] = props["title"]
            if "index" in fields:
                self.tabs.remove(tab)
                self.tabs.insert(props["index"], tab)
            grid = props.get("gridProperties", {})
            tab["rows"] = grid.get("rowCount", tab["rows"])
            tab["cols"] = grid.get("columnCount", tab["cols"])
        elif kind == "addConditionalFormatRule":
            rule = body["rule"]
            self._by_id(rule["ranges"][0]["sheetId"])["rules"].append(rule)
        elif kind == "addProtectedRange":
            protected = body["protectedRange"]
            self._by_id(protected["range"]["sheetId"])["protected"].append(protected["description"])
        return {}

    # --- helpers for assertions ---
    def titles(self) -> list[str]:
        return [t["title"] for t in self.tabs]

    def header(self, title: str) -> list[str]:
        values = self._by_title(title)["values"]
        return self._trim(values[0]) if values else []

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [r[kind] for r in self.requests if kind in r]

    def validation(self, title: str, column: int) -> dict[str, Any] | None:
        sheet_id = self._by_title(title)["id"]
        for body in self.of_kind("setDataValidation"):
            rng = body["range"]
            if rng["sheetId"] == sheet_id and rng["startColumnIndex"] == column:
                return body["rule"]  # type: ignore[no-any-return]
        return None
