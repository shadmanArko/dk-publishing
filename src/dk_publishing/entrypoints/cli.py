"""Admin commands: `dk migrate`."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from dk_publishing import composition


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dk")
    parser.add_argument("command", choices=["migrate"])
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required and has no default.", file=sys.stderr)
        return 2

    if args.command == "migrate":
        applied = composition.migrate_database(database_url)
        print("\n".join(f"applied {name}" for name in applied) or "already up to date")
    return 0
