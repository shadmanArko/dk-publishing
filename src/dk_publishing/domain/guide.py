"""What each platform accepts, as data a client can read.

The numbers come from the adapters' own constants (adapters/platforms/guides.py), and a test checks
that every example here passes the adapter's `validate`, so this cannot drift from the real rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldGuide:
    name: str
    kind: str  # text | choice | bool | number
    required: bool = False
    choices: tuple[str, ...] = ()
    max_length: int | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class MediaGuide:
    min: int
    max: int
    types: tuple[str, ...] = ()
    note: str = ""
    max_bytes: int | None = None  # per file; checked when a post is submitted


@dataclass(frozen=True, slots=True)
class FormatGuide:
    name: str
    summary: str
    media: MediaGuide
    caption: FieldGuide | None  # None: this format carries no caption
    fields: tuple[FieldGuide, ...]
    example: Mapping[str, Any]  # a variant that passes validation, with placeholder media

    def field(self, name: str) -> FieldGuide | None:
        if name == "caption":
            return self.caption
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class PlatformGuide:
    platform: str
    summary: str
    formats: tuple[FormatGuide, ...]
    notes: tuple[str, ...] = ()

    def format(self, name: str) -> FormatGuide | None:
        return next((f for f in self.formats if f.name == name), None)
