"""Loads config/sheet.yaml: what each Sheet tab and column is."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dk_publishing.adapters.config.platforms import ConfigError

KINDS = {
    "text",
    "long_text",
    "checkbox",
    "number",
    "date_time",
    "dropdown",
    "list",
    "account",
    "post_key",
    "unique_key",
    "status",
    "url",
    "timestamp",
}
SYSTEM_KINDS = {"status", "url", "timestamp"}  # always written by the system
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    kind: str
    options: tuple[str, ...] = ()
    list: str | None = None  # name of a column on the _lists tab
    required: bool = False
    system: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class SheetLayout:
    timezone: str
    rows: int
    slot_format: str
    posts: tuple[Column, ...]
    platform_before: tuple[Column, ...]
    platform_after: tuple[Column, ...]
    platform_extras: Mapping[str, tuple[Column, ...]]
    calendar: tuple[Column, ...]
    lists: tuple[Column, ...]
    readme: tuple[str, ...] = field(default_factory=tuple)

    def platform_columns(self, platform: str) -> tuple[Column, ...]:
        extras = self.platform_extras.get(platform, ())
        return (*self.platform_before, *extras, *self.platform_after)


def load_sheet_layout(path: Path, platform_keys: set[str]) -> SheetLayout:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path} must be a mapping")
    try:
        sheet = raw["sheet"]
        layout = SheetLayout(
            timezone=str(sheet["timezone"]),
            rows=int(sheet["rows"]),
            slot_format=str(sheet["slot_format"]),
            posts=_columns(raw["posts"], "posts"),
            platform_before=_columns(raw["platform_before"], "platform_before"),
            platform_after=_columns(raw["platform_after"], "platform_after"),
            platform_extras={
                key: _columns(cols, f"platform_extras.{key}")
                for key, cols in (raw.get("platform_extras") or {}).items()
            },
            calendar=_columns(raw["calendar"], "calendar"),
            lists=_columns(raw.get("lists") or [], "lists"),
            readme=tuple(str(line) for line in raw.get("readme") or []),
        )
    except KeyError as exc:
        raise ConfigError(f"{path}: missing section {exc}") from None

    unknown = set(layout.platform_extras) - platform_keys
    if unknown:
        raise ConfigError(
            f"sheet.yaml has extras for platforms not in platforms.yaml: {sorted(unknown)}"
        )
    list_names = {c.name for c in layout.lists}
    every = [*layout.posts, *layout.calendar]
    for key in platform_keys:
        every.extend(layout.platform_columns(key))
    for column in every:
        if column.kind == "list" and column.list not in list_names:
            raise ConfigError(f"column {column.name!r} uses unknown list {column.list!r}")
    for key in platform_keys:
        _unique(layout.platform_columns(key), f"platform {key!r}")
    _unique(layout.posts, "posts")
    _unique(layout.calendar, "calendar")
    return layout


def _columns(items: Any, where: str) -> tuple[Column, ...]:
    if not isinstance(items, list):
        raise ConfigError(f"{where} must be a list of columns")
    columns = tuple(_column(item, where) for item in items)
    _unique(columns, where)
    return columns


def _column(item: Any, where: str) -> Column:
    if not isinstance(item, Mapping) or "name" not in item or "kind" not in item:
        raise ConfigError(f"{where}: every column needs a name and a kind")
    name, kind = str(item["name"]), str(item["kind"])
    if not _NAME.match(name):
        raise ConfigError(f"{where}: column name {name!r} must be lower_snake_case")
    if kind not in KINDS:
        raise ConfigError(f"{where}.{name}: unknown kind {kind!r}")
    options = tuple(str(o) for o in item.get("options") or ())
    if kind == "dropdown" and not options:
        raise ConfigError(f"{where}.{name}: a dropdown needs options")
    if kind == "list" and not item.get("list"):
        raise ConfigError(f"{where}.{name}: a list column needs `list: <name>`")
    return Column(
        name=name,
        kind=kind,
        options=options,
        list=item.get("list"),
        required=bool(item.get("required", False)),
        system=bool(item.get("system", False)) or kind in SYSTEM_KINDS,
        note=str(item.get("note", "")),
    )


def _unique(columns: tuple[Column, ...], where: str) -> None:
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            raise ConfigError(f"{where}: duplicate column {column.name!r}")
        seen.add(column.name)
