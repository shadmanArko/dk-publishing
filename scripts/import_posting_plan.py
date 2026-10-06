"""Turn a posting plan (`posting-plan.xlsx`) into rows of the DK Publishing Sheet.

Preview, then write (needs openpyxl, which is not a project dependency):

    uv run --with openpyxl --env-file .env python scripts/import_posting_plan.py PLAN.xlsx
    uv run --with openpyxl --env-file .env python scripts/import_posting_plan.py PLAN.xlsx --write

Media files must already be in the Drive folder under the names `<plan #>_<original name>`. A post
that is already on the Posts tab is skipped, so the script can be run again. This script posts
nothing: ticking `ready` is what the system treats as approval, and the server then publishes at
each slot.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import openpyxl
from googleapiclient.errors import HttpError

from dk_publishing import composition as c
from dk_publishing.adapters.config.platforms import load_platforms
from dk_publishing.adapters.config.sheet_layout import load_sheet_layout
from dk_publishing.adapters.sheets.gateway import GoogleSheetGateway
from dk_publishing.adapters.sheets.google_access import connect
from dk_publishing.domain.timezones import utc_to_berlin

PLATFORMS = ("Facebook", "Instagram", "YouTube", "Threads", "TikTok")
ACCOUNT = "Dhaka Kacchi"
NEXT_SATURDAY_FROM = "2026-10-10"  # posts on or after this day (until the 12th) say "next Saturday"
NEXT_SATURDAY_UNTIL = "2026-10-12"


@dataclass
class Entry:
    number: int
    date: str
    time: str
    platform: str
    fmt: str
    theme: str
    media: list[str]
    title: str
    caption: str
    notes: str
    changes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"DK-2610-{self.number:02d}"

    @property
    def slot(self) -> datetime:
        return datetime.strptime(f"{self.date} {self.time}", "%Y-%m-%d %H:%M")


def read_plan(path: Path) -> list[Entry]:
    sheet = openpyxl.load_workbook(path, data_only=True)["Posting plan"]
    entries = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        number, date, _, time, platform, fmt, theme, media, title, caption, notes, _, _ = row
        if platform not in PLATFORMS:
            continue
        flat = [f"{int(number):02d}_{name.strip()}" for name in str(media).split(",")]
        entries.append(
            Entry(
                int(number), str(date), str(time), platform, str(fmt), str(theme), flat,
                str(title or ""), str(caption or ""), str(notes or ""),
            )
        )  # fmt: skip
    return entries


def polish(entries: list[Entry]) -> None:
    """The three agreed caption fixes. Everything else is left exactly as written."""
    seen_titles: dict[str, int] = {}
    for e in entries:
        if "link in the first comment 👇" in e.caption:
            e.caption = e.caption.replace(
                "link in the first comment 👇", "dhakakacchi.com"
            )  # the API cannot post a first comment
            e.changes.append("link now in the caption (no first comment)")
        if (
            NEXT_SATURDAY_FROM <= e.date <= NEXT_SATURDAY_UNTIL
            and "Order for Saturday" in e.caption
        ):
            e.caption = e.caption.replace("Order for Saturday", "Order for next Saturday")
            e.changes.append("'next Saturday'")
        if e.platform == "YouTube" and e.title:
            seen_titles[e.title] = seen_titles.get(e.title, 0) + 1
    repeated = {t for t, n in seen_titles.items() if n > 1}
    for e in entries:
        if e.platform == "YouTube" and e.title in repeated:
            day = datetime.strptime(e.date, "%Y-%m-%d").strftime("%a %-d %b")
            base = e.title.removesuffix(" #shorts")
            e.title = f"{base} · {day} #shorts"
            e.changes.append("unique title")


def spread_past_slots(entries: list[Entry], now: datetime, lead: int) -> list[str]:
    """A slot already past or too close moves to `lead` minutes from now, 3 minutes apart."""
    moved: list[str] = []
    earliest = now + timedelta(minutes=lead)
    step = 0
    for e in sorted(entries, key=lambda x: (x.slot, x.number)):
        if e.slot < earliest:
            old = e.slot
            e.date, e.time = (
                (earliest + timedelta(minutes=3 * step)).strftime("%Y-%m-%d %H:%M").split()
            )
            step += 1
            e.changes.append(f"time moved from {old:%d.%m %H:%M}")
            moved.append(f"{e.key} {e.platform} {e.fmt}: {old:%d.%m %H:%M} -> {e.date} {e.time}")
    return moved


def platform_cells(e: Entry) -> dict[str, Any]:
    """The platform tab's own columns; caption and slot are left blank to use the Posts row's."""
    base: dict[str, Any] = {"post_key": e.key, "enabled": True, "account": ACCOUNT}
    if e.platform == "Facebook":
        return {**base, "format": "photo" if "photo" in e.fmt.lower() else "video"}
    if e.platform == "Instagram":
        fmt = {"Carousel": "carousel", "Story": "story", "Reel": "reel"}[e.fmt]
        return {**base, "format": fmt, **({"share_to_feed": True} if fmt == "reel" else {})}
    if e.platform == "Threads":
        return {**base, "format": "image"}
    if e.platform == "YouTube":
        return {
            **base, "title": e.title, "description": e.caption,
            "made_for_kids": "no", "visibility_after_publish": "public",
        }  # fmt: skip
    return {  # TikTok: posted by hand from a Telegram card; these are the choices shown on it
        **base, "privacy_level": "public", "allow_comments": True, "allow_duet": True,
        "allow_stitch": True, "commercial_disclosure": True,
    }  # fmt: skip


def post_cells(e: Entry) -> dict[str, Any]:
    return {
        "post_key": e.key,
        "title": f"#{e.number} {e.platform} {e.fmt}: {e.theme}",
        "media": ", ".join(e.media),
        "default_caption": e.caption,
        "default_slot": f"{e.date} {e.time}",
        "ready": True,
    }


def patiently(call: Any, *args: Any) -> None:
    """Google allows 60 sheet writes a minute: pace the writes, and wait out a 'too fast' answer."""
    for attempt in range(8):
        try:
            call(*args)
            time.sleep(1.3)
            return
        except HttpError as exc:
            if exc.status_code != 429:
                raise
            time.sleep(20 + 10 * attempt)
    raise RuntimeError("Google kept refusing the writes as too fast")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--write", action="store_true", help="write the rows (default: preview)")
    parser.add_argument("--min-lead", type=int, default=20, help="minutes a slot must be ahead")
    parser.add_argument("--only", help="comma-separated platforms, e.g. Facebook,Threads")
    args = parser.parse_args()

    entries = read_plan(args.plan)
    if args.only:
        wanted = {p.strip() for p in args.only.split(",")}
        entries = [e for e in entries if e.platform in wanted]
    polish(entries)
    now = utc_to_berlin(datetime.now(UTC)).replace(tzinfo=None)
    moved = spread_past_slots(entries, now, args.min_lead)

    print(f"{len(entries)} posts for {', '.join(PLATFORMS)}")
    print("changed captions/titles/times:")
    for e in entries:
        if e.changes:
            print(f"  {e.key} {e.platform:<9} {', '.join(e.changes)}")
    if moved:
        print(f"{len(moved)} slots were already past or too close and were moved:")
        print("\n".join(f"  {m}" for m in moved))
    if not args.write:
        print("\nPreview only. Add --write to put these rows into the Sheet.")
        return 0

    env = c.environment()
    platforms = load_platforms(c.DEFAULT_PLATFORMS_CONFIG)
    layout = load_sheet_layout(c.DEFAULT_SHEET_CONFIG, set(platforms))
    sheets, _, _, _ = connect(Path(env["GOOGLE_APPLICATION_CREDENTIALS"]))
    gateway = GoogleSheetGateway(
        sheets, sheet_id=env["GOOGLE_SHEET_ID"], layout=layout, platforms=platforms
    )
    snapshot = gateway.read()
    existing = {p.post_key for p in snapshot.posts}
    has_platform_row = {(r.tab, r.post_key) for r in snapshot.rows}
    written = 0
    for e in sorted(entries, key=lambda x: x.number):
        if e.key in existing:
            continue
        tab = platforms[e.platform.lower()].tab
        if (tab, e.key) not in has_platform_row:  # never create a second row for the same post
            patiently(gateway.append_row, tab, c._sample_cells(platform_cells(e)))
        patiently(
            gateway.append_row, "Posts", c._sample_cells(post_cells(e))
        )  # last: ready=approval
        written += 1
    print(f"wrote {written} posts ({len(entries) - written} were already there)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
