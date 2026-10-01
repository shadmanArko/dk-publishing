from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from dk_publishing.adapters.config.platforms import (
    ConfigError,
    Mode,
    load_platforms,
    parse_duration,
)
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG

H = timedelta(hours=1)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "platforms.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("10m", timedelta(minutes=10)),
        ("2h", 2 * H),
        ("30d", timedelta(days=30)),
    ],
)
def test_durations(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "10", "m10", "1.5h", "2 hours", "-5m"])
def test_bad_durations_are_refused(bad: str) -> None:
    with pytest.raises(ConfigError, match="not a duration"):
        parse_duration(bad)


def test_the_shipped_config_loads_and_every_platform_is_valid() -> None:
    platforms = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    assert len(platforms) == 8
    assert {p.mode for p in platforms.values()} <= {Mode.DRY_RUN, Mode.OFF}
    assert all(p.capabilities.max_lateness == 2 * H for p in platforms.values())


def test_defaults_apply_and_entries_override_them(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
defaults: {max_lateness: 2h, prepare_lead: 30m}
platforms:
  a: {mode: dry_run}
  b: {mode: dry_run, max_lateness: 15m, native_window: [10m, 30d], prepared_ttl: 24h}
""",
    )
    a, b = load_platforms(path)["a"], load_platforms(path)["b"]
    assert (
        a.capabilities.prepare_lead == timedelta(minutes=30)
        and a.capabilities.native_window is None
    )
    assert b.capabilities.max_lateness == timedelta(minutes=15)
    assert b.capabilities.native_window == (timedelta(minutes=10), timedelta(days=30))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("a: {mode: live-ish}", "mode must be one of"),
        ("a: {mode: dry_run, colour: red}", "unknown keys"),
        ("a: {mode: dry_run, native_window: [10m]}", "native_window must be"),
        ("a: {mode: dry_run, prepare_lead: 2d, prepared_ttl: 1h}", "lifetime"),
        ("a: {mode: dry_run, max_lateness: 0s}", "positive"),
    ],
)
def test_a_bad_platform_names_itself(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=rf"platform 'a'.*{message}"):
        load_platforms(write(tmp_path, f"platforms:\n  {body}\n"))


def test_a_file_without_platforms_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="top-level `platforms`"):
        load_platforms(write(tmp_path, "defaults: {}\n"))


def test_an_unquoted_off_means_off_not_false(tmp_path: Path) -> None:
    """YAML turns a bare `off` into the boolean False; the loader must still read it as off."""
    path = write(tmp_path, "platforms:\n  a: {mode: off}\n  b: {mode: dry_run}\n")
    assert load_platforms(path)["a"].mode is Mode.OFF
    with pytest.raises(ConfigError, match="mode must be one of"):
        load_platforms(write(tmp_path, "platforms:\n  a: {mode: true}\n"))


def test_every_platform_has_a_sheet_tab_defaulting_to_its_key(tmp_path: Path) -> None:
    path = write(tmp_path, "platforms:\n  a: {mode: dry_run, tab: Alpha}\n  b: {mode: dry_run}\n")
    loaded = load_platforms(path)
    assert (loaded["a"].tab, loaded["b"].tab) == ("Alpha", "b")
    shipped = load_platforms(DEFAULT_PLATFORMS_CONFIG)
    assert len({p.tab for p in shipped.values()}) == len(shipped)  # no two share a tab
