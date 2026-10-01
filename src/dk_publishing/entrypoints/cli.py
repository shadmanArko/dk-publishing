"""Admin commands: `dk migrate`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import timedelta

from dk_publishing import composition


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dk")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply database migrations")
    rehearsal = commands.add_parser(
        "seed-rehearsal", help="create approved dry-run posts a few minutes from now"
    )
    rehearsal.add_argument("--count", type=int, default=4)
    rehearsal.add_argument("--spacing-minutes", type=int, default=2)
    rehearsal.add_argument(
        "--platforms", default="", help="comma-separated; default: every platform switched on"
    )
    args = parser.parse_args(argv)

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
