"""Loads config/platforms.yaml into typed per-platform settings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from dk_publishing.domain.capabilities import Capabilities

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_KNOWN = {
    "mode",
    "native_window",
    "prepare_lead",
    "prepared_ttl",
    "pulls_media_by_url",
    "max_lateness",
}


class ConfigError(Exception):
    """The configuration is unusable. Raised at start-up so a bad file never reaches a run."""


class Mode(StrEnum):
    OFF = "off"
    DRY_RUN = "dry_run"
    ASSISTED = "assisted"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class PlatformSettings:
    mode: Mode
    capabilities: Capabilities


def parse_duration(value: object) -> timedelta:
    match = _DURATION.match(str(value).strip())
    if not match:
        raise ConfigError(f"not a duration like 30s, 10m, 2h or 7d: {value!r}")
    return timedelta(**{_UNITS[match[2]]: int(match[1])})


def load_platforms(path: Path) -> dict[str, PlatformSettings]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, Mapping) or not isinstance(raw.get("platforms"), Mapping):
        raise ConfigError(f"{path} needs a top-level `platforms` mapping")
    defaults: Mapping[str, Any] = raw.get("defaults") or {}

    result: dict[str, PlatformSettings] = {}
    for name, entry in raw["platforms"].items():
        try:
            result[name] = _platform({**defaults, **(entry or {})})
        except (ConfigError, ValueError) as exc:
            raise ConfigError(f"platform {name!r}: {exc}") from exc
    return result


def _platform(entry: Mapping[str, Any]) -> PlatformSettings:
    unknown = set(entry) - _KNOWN
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}")
    raw_mode = entry.get("mode", "off")
    if raw_mode is False:
        raw_mode = "off"  # YAML reads an unquoted `off` as the boolean False
    try:
        mode = Mode(raw_mode)
    except ValueError:
        raise ConfigError(f"mode must be one of {[m.value for m in Mode]}") from None

    window = entry.get("native_window")
    if window is not None and (not isinstance(window, list) or len(window) != 2):
        raise ConfigError("native_window must be [min, max]")
    ttl = entry.get("prepared_ttl")

    capabilities = Capabilities(
        native_window=None
        if window is None
        else (parse_duration(window[0]), parse_duration(window[1])),
        prepare_lead=parse_duration(entry.get("prepare_lead", "30m")),
        prepared_ttl=None if ttl is None else parse_duration(ttl),
        pulls_media_by_url=bool(entry.get("pulls_media_by_url", False)),
        max_lateness=parse_duration(entry.get("max_lateness", "2h")),
    )
    return PlatformSettings(mode, capabilities)
