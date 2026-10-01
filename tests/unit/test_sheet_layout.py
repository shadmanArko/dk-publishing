from __future__ import annotations

from pathlib import Path

import pytest

from dk_publishing.adapters.config.platforms import ConfigError, load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG, DEFAULT_SHEET_CONFIG

KEYS = {"a"}
BASE = """
sheet: {timezone: Europe/Berlin, rows: 100, slot_format: "dd.MM.yyyy HH:mm"}
posts: [{name: post_key, kind: unique_key}]
platform_before: [{name: post_key, kind: post_key}]
platform_after: [{name: status, kind: status}]
calendar: [{name: slot, kind: timestamp}]
"""


def load(tmp_path: Path, text: str, keys: set[str] = KEYS):  # type: ignore[no-untyped-def]
    path = tmp_path / "sheet.yaml"
    path.write_text(text)
    return load_sheet_layout(path, keys)


def test_the_shipped_layout_loads_for_every_platform() -> None:
    platforms = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    layout = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(platforms))
    for key in platforms:
        names = [c.name for c in layout.platform_columns(key)]
        assert names[:5] == ["post_key", "enabled", "account", "caption", "slot"]
        assert names[-4:] == ["status", "live_url", "last_error", "synced_at"]
    assert layout.timezone == "Europe/Berlin" and layout.rows == 1000


def test_system_kinds_are_always_grey() -> None:
    layout = load_sheet_layout(DEFAULT_SHEET_CONFIG, set(load_platforms(DEFAULT_PLATFORMS_CONFIG)))
    assert all(c.system for c in layout.platform_after)
    assert not any(c.system for c in layout.platform_before)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("posts: [{name: Post Key, kind: text}]", "lower_snake_case"),
        ("posts: [{name: a, kind: sparkly}]", "unknown kind"),
        ("posts: [{name: a, kind: dropdown}]", "needs options"),
        ("posts: [{name: a, kind: list}]", "needs `list:"),
        ("posts: [{name: a, kind: text}, {name: a, kind: text}]", "duplicate column"),
        ("posts: [{kind: text}]", "needs a name and a kind"),
        ("posts: [{name: a, kind: list, list: nope}]", "unknown list"),
        ("platform_extras: {zzz: [{name: q, kind: text}]}", "not in platforms.yaml"),
        ("platform_extras: {a: [{name: status, kind: text}]}", "duplicate column"),
    ],
)
def test_bad_layouts_are_refused_with_a_reason(tmp_path: Path, patch: str, message: str) -> None:
    lines = [line for line in BASE.splitlines() if not line.startswith(patch.split(":")[0] + ":")]
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, "\n".join(lines) + "\n" + patch + "\n")


def test_a_missing_section_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing section"):
        load(tmp_path, "sheet: {timezone: Europe/Berlin, rows: 1, slot_format: x}\n")
