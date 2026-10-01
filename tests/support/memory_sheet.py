"""A stand-in for the Sheet gateway and the Drive catalog, for testing the sync's decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from dk_publishing.domain.publishing import Violation
from dk_publishing.domain.sheet import (
    CalendarEntry,
    MediaFile,
    PlatformRow,
    PostRow,
    RawRow,
    SheetSnapshot,
    StatusPlan,
)

POSTS_TAB = "Posts"
TAB = "Platform P"


class MemorySheet:
    """Holds rows the way a person would type them, and remembers what the system wrote back.

    Written status is fed into the next `read()`, exactly as the real Sheet would, so a second
    sync sees what the first one wrote.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.system: dict[tuple[str, int], dict[str, Any]] = {}
        self.plans: list[StatusPlan] = []
        self.calendar: list[CalendarEntry] = []
        self.accounts: dict[str, list[str]] = {}
        self.tab_problems: list[str] = []
        self._next_row = {POSTS_TAB: 2, TAB: 2}

    def add_post(self, post_key: str, **fields: Any) -> dict[str, Any]:
        row = fields.pop("row", None) or self._take(POSTS_TAB)
        post = {
            "row": row, "post_key": post_key, "title": "", "media": (),
            "default_caption": "Fresh from the pot", "default_slot": None,
            "ready": False, "problems": (),
        }  # fmt: skip
        post.update(fields)
        self.posts.append(post)
        return post

    def add_row(self, post_key: str, **fields: Any) -> dict[str, Any]:
        row = fields.pop("row", None) or self._take(TAB)
        entry = {
            "tab": TAB, "platform": "p", "row": row, "post_key": post_key, "enabled": True,
            "account": "Main", "caption": "", "slot": None, "options": {"format": "reel"},
            "problems": (),
        }  # fmt: skip
        entry.update(fields)
        self.rows.append(entry)
        return entry

    def post(self, post_key: str) -> dict[str, Any]:
        return next(p for p in self.posts if p["post_key"] == post_key)

    def row(self, post_key: str, *, index: int = 0) -> dict[str, Any]:
        return [r for r in self.rows if r["post_key"] == post_key][index]

    def delete_rows(self, post_key: str) -> None:
        self.rows = [r for r in self.rows if r["post_key"] != post_key]

    def status(self, post_key: str, index: int = 0) -> dict[str, Any]:
        r = self.row(post_key, index=index)
        return self.system.get((r["tab"], r["row"]), {})

    def post_status(self, post_key: str) -> dict[str, Any]:
        return self.system.get((POSTS_TAB, self.post(post_key)["row"]), {})

    def _take(self, tab: str) -> int:
        row = self._next_row[tab]
        self._next_row[tab] += 1
        return row

    # --- SheetGateway ---
    def read(self) -> SheetSnapshot:
        out = SheetSnapshot(problems=list(self.tab_problems))
        for p in self.posts:
            out.posts.append(
                PostRow(
                    row=p["row"], post_key=p["post_key"], title=p["title"], media=tuple(p["media"]),
                    default_caption=p["default_caption"], default_slot=p["default_slot"],
                    ready=p["ready"], problems=tuple(p["problems"]),
                    system=dict(self.system.get((POSTS_TAB, p["row"]), {})),
                )
            )  # fmt: skip
            out.raw.append(RawRow(POSTS_TAB, p["row"], {"post_key": p["post_key"]}))
        for r in self.rows:
            out.rows.append(
                PlatformRow(
                    tab=r["tab"], platform=r["platform"], row=r["row"], post_key=r["post_key"],
                    enabled=r["enabled"], account=r["account"], caption=r["caption"],
                    slot=r["slot"], options=dict(r["options"]), problems=tuple(r["problems"]),
                    system=dict(self.system.get((r["tab"], r["row"]), {})),
                )
            )  # fmt: skip
            out.raw.append(RawRow(r["tab"], r["row"], {"post_key": r["post_key"]}))
        return out

    def write(self, plan: StatusPlan) -> int:
        self.plans.append(plan)
        cells = 0
        for s in plan.rows:
            self.system[(s.tab, s.row)] = {
                "status": s.status, "live_url": s.live_url, "last_error": s.last_error,
                "synced_at": s.synced_at,
            }  # fmt: skip
            cells += 4
        for p in plan.posts:
            self.system[(POSTS_TAB, p.row)] = {"status": p.status, "last_error": p.last_error}
            cells += 2
        if plan.calendar is not None:
            self.calendar = list(plan.calendar)
        if plan.accounts is not None:
            self.accounts = {k: list(v) for k, v in plan.accounts.items()}
        return cells


class FakeMedia:
    def __init__(self, *names: str) -> None:
        self.files = [MediaFile(f"id-{n}", n, f"md5-{n}") for n in names]

    def reset(self, *names: str) -> None:
        self.files = [MediaFile(f"id-{n}", n, f"md5-{n}") for n in names]

    def replace(self, name: str, md5: str) -> None:
        self.files = [MediaFile(f.id, f.name, md5) if f.name == name else f for f in self.files]

    def remove(self, name: str) -> None:
        self.files = [f for f in self.files if f.name != name]

    def list_files(self) -> list[MediaFile]:
        return list(self.files)


def problem(field: str, message: str) -> Violation:
    return Violation(field, message)


def naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)
