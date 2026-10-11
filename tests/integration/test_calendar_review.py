"""Approving or rejecting posts an agent submitted, by ticking a box in the Sheet's Calendar tab."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from dk_publishing.application.services import SyncServices
from dk_publishing.application.use_cases.sync_sheet import SyncReport, sync_sheet
from dk_publishing.domain.sheet import CalendarDecision, CalendarEntry
from tests.integration.conftest import TENANT
from tests.integration.test_api import Rig as ApiRig
from tests.support.memory_sheet import FakeMedia, MemorySheet


class Review:
    """The API rig plus a stand-in Sheet, so ticks and the Calendar can be exercised together."""

    def __init__(self, conninfo: str, tmp_path: Path) -> None:
        self.api = ApiRig(conninfo, tmp_path)
        self.sheet = MemorySheet()
        self.services = SyncServices(
            core=self.api.api.core,
            sheet=self.sheet,
            media=FakeMedia(),
            platforms=["instagram", "facebook", "threads", "youtube", "tiktok"],
            tenant_id=TENANT,
        )

    def submit(self, key: str = "agent-1", **more: object) -> None:
        assert self.api.post(self.api.reel(key, **more)).status_code == 201

    def sync(self) -> SyncReport:
        return sync_sheet(self.services)

    def entry(self, key: str = "agent-1") -> CalendarEntry:
        [found] = [e for e in self.sheet.calendar if e.post_key == key]
        return found

    def tick(self, key: str = "agent-1", *, approve: bool = False, reject: bool = False) -> None:
        self.sheet.decisions.append(
            CalendarDecision(2, key, "instagram", "Dhaka Kacchi", approve, reject)
        )

    def status(self, key: str = "agent-1") -> str:
        return str(self.api.call("GET", f"/v1/posts/{key}", "read").json()["summary"])


@pytest.fixture
def review(conninfo: str, tmp_path: Path) -> Review:
    return Review(conninfo, tmp_path)


def test_an_agent_draft_appears_in_the_calendar_with_what_a_person_needs_to_decide(
    review: Review,
) -> None:
    review.submit()
    review.sync()
    entry = review.entry()
    assert (entry.source, entry.status, entry.by) == ("agent", "awaiting_approval", "draft-key")
    assert (entry.platform, entry.format, entry.caption) == ("instagram", "reel", "Slow-cooked.")
    assert entry.media == "reel.mp4" and "approve or reject" in entry.note


def test_ticking_approve_approves_the_draft_through_the_normal_path(review: Review) -> None:
    review.submit()
    review.sync()
    review.tick(approve=True)
    report = review.sync()
    assert report.reviewed_approved == 1 and review.status() == "scheduled"
    assert review.entry().status in ("approved", "prepared")
    assert review.entry().note == ""
    assert review.sheet.decisions == []  # the tick was cleared with the rewrite
    event = review.api.call("GET", "/v1/posts/agent-1", "read").json()
    assert event["variants"][0]["status"] != "draft"


def test_ticking_reject_cancels_it(review: Review) -> None:
    review.submit()
    review.sync()
    review.tick(reject=True)
    report = review.sync()
    assert report.reviewed_rejected == 1 and review.status() == "cancelled"
    assert not [e for e in review.sheet.calendar if e.post_key == "agent-1"]


def test_both_ticks_do_nothing_and_say_why(review: Review) -> None:
    review.submit()
    review.sync()
    review.tick(approve=True, reject=True)
    report = review.sync()
    assert any("not both" in p for p in report.problems)
    assert review.status() == "awaiting_approval"


def test_a_tick_after_the_slot_has_passed_is_refused(review: Review) -> None:
    review.submit()
    review.api.clock.advance(timedelta(days=3))
    review.sync()
    assert "slot has passed" in review.entry().note
    review.tick(approve=True)
    report = review.sync()
    assert any("slot has passed" in p for p in report.problems)
    assert review.status() == "awaiting_approval"
    review.tick(reject=True)
    review.sync()
    assert review.status() == "cancelled"


def test_a_tick_on_something_that_already_moved_on_is_ignored(review: Review) -> None:
    review.submit()
    review.sync()
    assert review.api.call("POST", "/v1/posts/agent-1/approve", "approve").status_code == 200
    review.tick(reject=True)  # a stale tick from before the approval
    report = review.sync()
    assert report.reviewed_rejected == 0 and review.status() == "scheduled"


def test_humans_posts_and_unknown_rows_are_never_touched_by_a_tick(review: Review) -> None:
    review.submit()
    review.sync()
    review.tick("no-such-post", approve=True)
    report = review.sync()
    assert report.reviewed_approved == 0 and review.status() == "awaiting_approval"


def test_the_calendar_is_sorted_by_date(review: Review) -> None:
    review.submit("later")
    review.api.clock.advance(timedelta(minutes=1))
    body = review.api.reel("sooner")
    body["variants"][0]["slot"] = (
        (review.api.clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    )
    assert review.api.post(body).status_code == 201
    review.sync()
    assert [e.post_key for e in review.sheet.calendar] == ["sooner", "later"]


def test_a_sync_with_nothing_new_leaves_the_calendar_as_it_was(review: Review) -> None:
    review.submit()
    review.sync()
    before = list(review.sheet.calendar)
    review.sync()
    assert review.sheet.calendar == before
