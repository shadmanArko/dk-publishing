"""Admin commands: `dk migrate`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from dk_publishing import composition
from dk_publishing.adapters.sheets.google_access import CredentialsError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dk")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations")
    commands.add_parser(
        "check-google", help="test the Google service account, Sheet and Drive folder"
    )
    rehearsal = commands.add_parser(
        "seed-rehearsal", help="create approved dry-run posts a few minutes from now"
    )
    rehearsal.add_argument("--count", type=int, default=4)
    rehearsal.add_argument("--spacing-minutes", type=int, default=2)
    rehearsal.add_argument(
        "--platforms", default="", help="comma-separated; default: every platform switched on"
    )
    args = parser.parse_args(argv)

    if args.command == "check-google":
        return _check_google()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required and has no default.", file=sys.stderr)
        return 2

    if args.command == "migrate":
        applied = composition.migrate_database(database_url)
        print("\n".join(f"applied {name}" for name in applied) or "already up to date")
    elif args.command == "seed-rehearsal":
        results = composition.seed_rehearsal(
            database_url,
            count=args.count,
            spacing=timedelta(minutes=args.spacing_minutes),
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()] or None,
        )
        print(f"approved {len(results)} variants; the sensor will publish them in dry-run")
    return 0


def _check_google() -> int:
    wanted = ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_SHEET_ID", "GOOGLE_DRIVE_FOLDER_ID")
    values = {name: os.environ.get(name, "").strip() for name in wanted}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print(f"Set in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        report, absent_tabs = composition.check_google(
            Path(values["GOOGLE_APPLICATION_CREDENTIALS"]).expanduser(),
            values["GOOGLE_SHEET_ID"],
            values["GOOGLE_DRIVE_FOLDER_ID"],
        )
    except CredentialsError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    print(f"[ok]   key loaded for {report.service_account_email}")
    if report.key_file_warning:
        print(f"[warn] {report.key_file_warning}")
    if report.sheet_title is not None:
        edit = {True: "can edit", False: "VIEW ONLY", None: "edit rights unknown"}[
            report.can_edit_sheet
        ]
        print(f"[ok]   Sheet {report.sheet_title!r}: {len(report.sheet_tabs)} tabs, {edit}")
        if absent_tabs:
            print(f"[todo] tabs not created yet: {', '.join(absent_tabs)} (sheet setup comes next)")
    if report.folder_name is not None:
        more = "+" if report.folder_files_truncated else ""
        print(f"[ok]   Drive folder {report.folder_name!r}: {report.folder_files}{more} items")
    for problem in report.problems:
        print(f"[FAIL] {problem}")
    return 0 if report.ok else 1
