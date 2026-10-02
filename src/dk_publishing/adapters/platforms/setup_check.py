"""`make check-setup`: read dk.json and test everything in it, saying plainly what is missing.

Nothing here posts. Sections left empty are reported as skipped, not as errors.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dk_publishing.adapters.config.platforms import ConfigError, Mode, PlatformSettings
from dk_publishing.adapters.notify.telegram import run_setup
from dk_publishing.adapters.platforms.connect import connect_from_env
from dk_publishing.adapters.platforms.dk_file import config_path, load
from dk_publishing.adapters.platforms.meta_check import check_meta
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials
from dk_publishing.adapters.sheets.google_access import CredentialsError, check_access, connect


@dataclass
class SetupReport:
    lines: list[str] = field(default_factory=list)
    problems: int = 0

    def ok(self, text: str) -> None:
        self.lines.append(f"[ok]   {text}")

    def skip(self, text: str) -> None:
        self.lines.append(f"[skip] {text}")

    def fail(self, text: str) -> None:
        self.problems += 1
        self.lines.append(f"[FAIL] {text}")


def _filled(section: Any) -> bool:
    """True if any non-help value in the section (or its subsections) has been filled in."""
    if isinstance(section, dict):
        return any(_filled(v) for k, v in section.items() if not k.startswith("_"))
    return bool(str(section or "").strip())


def check_setup(env: Mapping[str, str], platforms: Mapping[str, PlatformSettings]) -> SetupReport:
    report = SetupReport()
    path = config_path(env)
    if path is None:
        report.fail("DK_CONFIG_FILE is not set in .env; run `make init` and follow what it prints")
        return report
    try:
        data = load(path)
    except ConfigError as exc:
        report.fail(str(exc))
        return report
    report.ok(f"configuration file {path}")
    live = sorted(n for n, s in platforms.items() if s.mode is Mode.LIVE)
    report.ok(f"platforms switched on (config/platforms.yaml): {', '.join(live) or 'none yet'}")

    _google(report, env)
    _meta(report, env, data, platforms)
    _youtube(report, env, data)
    _telegram(report, env, data)
    _media(report, data)
    return report


def _google(report: SetupReport, env: Mapping[str, str]) -> None:
    key = env.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    sheet, folder = (
        env.get("GOOGLE_SHEET_ID", "").strip(),
        env.get("GOOGLE_DRIVE_FOLDER_ID", "").strip(),
    )
    missing = [
        n
        for n, v in (
            ("service_account_file", key),
            ("sheet_id", sheet),
            ("drive_folder_id", folder),
        )
        if not v
    ]
    if missing:
        report.fail(f"google: fill in {', '.join(missing)} (docs/setup/google.md)")
        return
    try:
        sheets, drive, email, warning = connect(Path(key).expanduser())
        access = check_access(sheets, drive, sheet_id=sheet, folder_id=folder, email=email)
    except CredentialsError as exc:
        report.fail(f"google: {exc}")
        return
    if warning:
        report.lines.append(f"[warn] {warning}")
    for problem in access.problems:
        report.fail(f"google: {problem}")
    if access.ok:
        report.ok(
            f"google: Sheet '{access.sheet_title}' and folder '{access.folder_name}' reachable"
        )


def _meta(
    report: SetupReport,
    env: Mapping[str, str],
    data: dict[str, Any],
    platforms: Mapping[str, PlatformSettings],
) -> None:
    if not _filled(data.get("meta")):
        report.skip("meta (Facebook, Instagram, Threads): nothing filled in (docs/setup/meta.md)")
        return
    result = check_meta(MetaCredentials(env["META_CREDENTIALS_FILE"]), platforms)
    report.lines.extend(f"{line}" for line in result.lines)
    report.problems += len(result.problems)


def _youtube(report: SetupReport, env: Mapping[str, str], data: dict[str, Any]) -> None:
    section = data.get("youtube")
    if not _filled(section):
        report.skip("youtube: nothing filled in (docs/setup/youtube.md)")
        return
    result = connect_from_env("youtube", env, check_only=True)
    report.lines.extend(result.lines)
    if not result.ok:
        report.problems += 1


def _telegram(report: SetupReport, env: Mapping[str, str], data: dict[str, Any]) -> None:
    if not _filled(data.get("telegram")):
        report.skip("telegram: nothing filled in; alerts stay off (docs/setup/telegram.md)")
        return
    result = run_setup("check", env["TELEGRAM_CREDENTIALS_FILE"])
    report.lines.extend(result.lines)
    if not result.ok:
        report.problems += 1


def _media(report: SetupReport, data: dict[str, Any]) -> None:
    if _filled(data.get("media")):
        report.ok("media: public link settings present (needed for Instagram/Threads photos)")
    else:
        report.skip("media: no public link settings; Instagram/Threads photos wait for the server")
