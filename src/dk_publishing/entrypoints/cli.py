"""Admin commands: migrate, sync, account, sheet init/sample, check-google, seed-rehearsal."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from googleapiclient.errors import HttpError

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.sheets.google_access import CredentialsError
from dk_publishing.application.ports import DuplicateAccount
from dk_publishing.application.use_cases.sync_sheet import sync_sheet


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dk")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations")
    commands.add_parser(
        "check-google", help="test the Google service account, Sheet and Drive folder"
    )
    sheet = commands.add_parser("sheet", help="Google Sheet commands")
    sheet_commands = sheet.add_subparsers(dest="sheet_command", required=True)
    init = sheet_commands.add_parser(
        "init", help="create or repair the Sheet layout (never overwrites data)"
    )
    init.add_argument(
        "--dry-run", action="store_true", help="show what would change; write nothing"
    )
    sheet_commands.add_parser("sample", help="add the sample post to the Sheet")
    sync = commands.add_parser("sync", help="read the Sheet and update posts now")
    sync.add_argument(
        "--allow-cancellations", action="store_true", help="skip the bulk-cancel guard"
    )
    meta = commands.add_parser("meta", help="the one-file Meta credentials")
    meta_commands = meta.add_subparsers(dest="meta_command", required=True)
    meta_commands.add_parser("init", help="create the one-file credentials template")
    meta_commands.add_parser("check", help="test the tokens without posting anything")
    meta_commands.add_parser(
        "page-token", help="swap the user token in the file for the Page's own token"
    )
    refresh = meta_commands.add_parser("refresh", help="renew the tokens that expire")
    refresh.add_argument("--force", action="store_true", help="renew even if refreshed recently")
    telegram = commands.add_parser("telegram", help="Telegram alerts: setup and tests")
    telegram.add_argument(
        "telegram_command",
        choices=["init", "chat", "check", "test"],
        help="init: create file; chat: find chat id; check: test bot; test: message you",
    )
    alerts = commands.add_parser("alerts", help="send pending alerts or the digest now")
    alerts.add_argument("what", choices=["pending", "digest"])
    connect = commands.add_parser("connect", help="one-time login setup for a platform")
    connect.add_argument("platform")
    connect.add_argument("--check", action="store_true", help="only test the saved login")
    account = commands.add_parser("account", help="dry-run accounts")
    account_commands = account.add_subparsers(dest="account_command", required=True)
    account_commands.add_parser("list", help="list accounts")
    add = account_commands.add_parser("add", help="add a dry-run account")
    add.add_argument("platform")
    add.add_argument("name", help="display name, as it appears in the Sheet dropdown")
    add.add_argument(
        "--external-id", help="the platform's own account id (see the platform's docs)"
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
    if args.command == "sheet":
        return _sample() if args.sheet_command == "sample" else _sheet_init(dry_run=args.dry_run)
    if args.command == "sync":
        return _sync(allow_cancellations=args.allow_cancellations)
    if args.command == "account":
        return _account(args)
    if args.command == "connect":
        return _connect(args.platform, check_only=args.check)
    if args.command == "telegram":
        return _telegram(args.telegram_command)
    if args.command == "meta":
        return _meta(args.meta_command, force=getattr(args, "force", False))

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required and has no default.", file=sys.stderr)
        return 2

    if args.command == "alerts":
        return _alerts(database_url, args.what)
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


def _sheet_init(*, dry_run: bool) -> int:
    path, sheet_id = (
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
        os.environ.get("GOOGLE_SHEET_ID", ""),
    )
    if not path.strip() or not sheet_id.strip():
        print("Set GOOGLE_APPLICATION_CREDENTIALS and GOOGLE_SHEET_ID in .env", file=sys.stderr)
        return 2
    try:
        report = composition.init_sheet(Path(path).expanduser(), sheet_id.strip(), dry_run=dry_run)
    except CredentialsError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    except HttpError as exc:
        print(
            f"[FAIL] Google refused the request ({exc.resp.status}). Run: make check-google",
            file=sys.stderr,
        )
        return 1

    verb = "would" if dry_run else "did"
    if report.timezone_set:
        print(f"[{verb}] set the Sheet time zone to {report.timezone_set}")
    for old, new in report.renamed:
        print(f"[{verb}] rename the blank tab {old!r} to {new!r}")
    if report.created:
        print(f"[{verb}] create {len(report.created)} tabs: {', '.join(report.created)}")
    for tab, added in report.headers_added.items():
        if added and tab not in report.created:
            print(f"[{verb}] add headers to {tab!r}: {', '.join(added)}")
    if report.readme_written:
        print(f"[{verb}] write the README text")
    if report.reordered:
        print(f"[{verb}] put the tabs in order")
    if report.formatted:
        print(
            f"[{verb}] (re)apply formatting, dropdowns and protection "
            f"on {len(report.formatted)} tabs"
        )
    if report.left_alone:
        print(f"[keep] left alone (not ours): {', '.join(report.left_alone)}")
    for problem in report.problems:
        print(f"[FAIL] {problem}")
    if dry_run:
        print("Dry run: nothing was written. Run without --dry-run to apply.")
    return 0 if not report.problems else 1


def _google_env() -> dict[str, str] | None:
    wanted = (
        "DATABASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_SHEET_ID",
        "GOOGLE_DRIVE_FOLDER_ID",
    )
    values = {name: os.environ.get(name, "").strip() for name in wanted}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print(f"Set in .env: {', '.join(missing)}", file=sys.stderr)
        return None
    return values


def _sync(*, allow_cancellations: bool) -> int:
    env = _google_env()
    if env is None:
        return 2
    try:
        services = composition.build_sync_services(
            env["DATABASE_URL"],
            Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).expanduser(),
            env["GOOGLE_SHEET_ID"],
            env["GOOGLE_DRIVE_FOLDER_ID"],
            env=os.environ,
        )
        report = sync_sheet(services, allow_cancellations=allow_cancellations)
    except CredentialsError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    except HttpError as exc:
        print(
            f"[FAIL] Google refused the request ({exc.resp.status}). Run: make check-google",
            file=sys.stderr,
        )
        return 1

    print(
        f"created {report.created}, approved {report.approved}, invalid {report.marked_invalid}, "
        f"approvals withdrawn {report.withdrawn}, cancelled {report.cancelled}, "
        f"already live {report.left_live}, busy {report.busy}; wrote {report.cells_written} cells"
    )
    for problem in report.problems:
        print(f"[warn] {problem}")
    for error in report.errors:
        print(f"[FAIL] {error}")
    if report.halted:
        print(f"[HALTED] {report.halted}")
    return 1 if (report.halted or report.errors) else 0


def _account(args: argparse.Namespace) -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required and has no default.", file=sys.stderr)
        return 2
    if args.account_command == "list":
        for a in composition.list_accounts(database_url):
            print(f"{a.platform:<10} {a.display_name or '(no name)':<30} {a.status}")
        return 0
    try:
        composition.add_account(
            database_url, args.platform, args.name, external_id=args.external_id
        )
    except (ConfigError, DuplicateAccount) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"added {args.name!r} for {args.platform}; it appears in the Sheet's account dropdown")
    return 0


def _sample() -> int:
    env = _google_env()
    if env is None:
        return 2
    try:
        done = composition.install_sample(
            env["DATABASE_URL"],
            Path(env["GOOGLE_APPLICATION_CREDENTIALS"]).expanduser(),
            env["GOOGLE_SHEET_ID"],
            env["GOOGLE_DRIVE_FOLDER_ID"],
        )
    except (CredentialsError, ConfigError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("\n".join(f"[did] {line}" for line in done))
    return 0


def _meta(command: str, *, force: bool = False) -> int:
    path = Path(os.environ.get("META_CREDENTIALS_FILE", "").strip() or meta_default()).expanduser()
    if command == "init":
        created = composition.meta_init(path, os.environ)
        if not created:
            print(f"{path} already exists; left it alone")
            return 0
        print(f"[did] created {path} (owner-only permissions, outside the repo)")
        print("Open it, paste each token between the quotes, then run: make meta-check")
        if not os.environ.get("META_CREDENTIALS_FILE", "").strip():
            print(f"Also add this line to .env:  META_CREDENTIALS_FILE={path}")
        return 0
    if command == "refresh":
        try:
            outcome = composition.meta_refresh(path, force=force)
        except ConfigError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1
        print(f"[{'ok' if outcome.ok else 'FAIL'}]   {outcome.message}")
        return 0 if outcome.ok else 1
    if command == "page-token":
        try:
            result = composition.meta_page_token(path)
        except ConfigError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 1
        print(f"[{'ok' if result.ok else 'FAIL'}]   {result.message}")
        if result.changed:
            print("Now run: make meta-check")
        return 0 if result.ok else 1
    try:
        report = composition.meta_check(path)
    except ConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("\n".join(report.lines))
    return 1 if report.problems else 0


def meta_default() -> str:
    from dk_publishing.adapters.platforms.meta_credentials import DEFAULT_PATH

    return str(DEFAULT_PATH)


def _connect(platform: str, *, check_only: bool) -> int:
    try:
        result = composition.connect_login(platform, os.environ, check_only=check_only)
    except ConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("\n".join(result.lines))
    return 0 if result.ok else 1


def _telegram(command: str) -> int:
    try:
        result = composition.telegram_setup(command, os.environ)
    except ConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("\n".join(result.lines))
    return 0 if result.ok else 1


def _alerts(database_url: str, what: str) -> int:
    from dk_publishing.application.ports import NotifyError
    from dk_publishing.application.use_cases.alerts import send_alerts, send_digest

    try:
        notifier = composition.build_notifier(os.environ)
    except ConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    if notifier is None:
        print("TELEGRAM_CREDENTIALS_FILE is not set; run `make telegram-init`", file=sys.stderr)
        return 1
    services = composition.build_alert_services(database_url)
    try:
        if what == "digest":
            sent = send_digest(services, notifier, composition.credential_warnings(os.environ))
            print("sent the digest" if sent else "today's digest was already sent")
        else:
            report = send_alerts(services, notifier)
            print(f"sent {report.sent} alert(s)")
            for problem in report.failed:
                print(f"[FAIL] {problem}", file=sys.stderr)
            return 1 if report.failed else 0
    except NotifyError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    return 0
