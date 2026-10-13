"""The owner's daily review page against a real database (ADR 0025).

Drafts are made the way an agent makes them (the batch endpoint with a draft key); the page then
approves and rejects them through the real use cases, so the assertions read the state back through
the API, the same way the Calendar and the agent would see it.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from dk_publishing.application.services import ReviewServices, SyncServices
from dk_publishing.application.use_cases import daily_review
from dk_publishing.application.use_cases.alerts import send_digest
from dk_publishing.application.use_cases.sync_sheet import sync_sheet
from dk_publishing.domain import review as tokens
from dk_publishing.domain.model import Variant
from dk_publishing.entrypoints.publish_api.app import create_app
from tests.integration.conftest import TENANT
from tests.integration.test_api import Rig
from tests.support import T0
from tests.support.fake_telegram import FakeNotifier
from tests.support.memory_sheet import FakeMedia, MemorySheet

SECRET = "k" * 43
BASE = "https://publish.example.test"
DAY = "2026-11-16"  # two days after the fake clock (T0 is Saturday 14 Nov 2026, noon UTC)
DAY_DATE = date(2026, 11, 16)


def at(hour: int, minute: int = 0) -> str:
    """A slot on DAY at this Berlin time (CET, +01:00 in November)."""
    return f"{DAY}T{hour:02d}:{minute:02d}:00+01:00"


class FakePreview:
    """Hands out a link per uploaded file, like the real one, and remembers what was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def url_for(self, item: Any) -> str | None:
        self.asked.append(str(item["name"]))
        return f"https://media.example.test/tok/{item['name']}"


class Review:
    def __init__(self, conninfo: str, tmp_path: Path, *, configured: bool = True) -> None:
        self.conninfo = conninfo
        self.rig = Rig(conninfo, tmp_path)
        self.preview = FakePreview()
        self.services = ReviewServices(
            core=self.rig.api.core,
            secret=SECRET,
            base_url=BASE,
            tenant_id=TENANT,
            preview=self.preview,
        )
        app = create_app(self.rig.api, None, self.services if configured else None)
        self.web = TestClient(app, base_url=BASE, follow_redirects=False)

    # --- the agent's side: drafts -----------------------------------------------------------

    def idea(
        self, key: str, *, hour: int = 13, platforms: tuple[str, ...] = ("instagram", "tiktok")
    ) -> None:
        media = self.rig.upload(f"{key}.mp4", data=key.encode() * 50)
        variants: list[dict[str, Any]] = []
        for platform in platforms:
            base: dict[str, Any] = {
                "platform": platform,
                "format": "reel" if platform == "instagram" else "video",
                "caption": f"Caption for {platform} <b>{key}</b>",
                "media": [media],
                "slot": at(hour),
            }
            if platform == "tiktok":
                base["options"] = {"privacy_level": "public"}
            if platform == "youtube":
                base["options"] = {"title": f"Title {key}", "made_for_kids": "no"}
            variants.append(base)
        answer = self.rig.call(
            "POST", "/v1/posts/batch", json={"posts": [{"post_key": key, "variants": variants}]}
        )
        assert answer.json()["ok"] == 1, answer.text

    def state(self, key: str) -> dict[str, str]:
        """platform -> the API's state word for that post."""
        post = self.rig.call("GET", f"/v1/posts/{key}", "read").json()
        return {v["platform"]: v["state"] for v in post["variants"]}

    def ids(self, key: str) -> dict[str, str]:
        post = self.rig.call("GET", f"/v1/posts/{key}", "read").json()
        return {v["platform"]: v["variant_id"] for v in post["variants"]}

    # --- the owner's side ---------------------------------------------------------------------

    def link(self, **more: Any) -> str:
        args = {
            "secret": SECRET,
            "purpose": tokens.LINK,
            "now": self.rig.clock.now(),
            "ttl": tokens.LINK_TTL,
        }
        return tokens.issue(**{**args, **more})

    def sign_in(self) -> None:
        answer = self.web.get(f"/review/enter?t={self.link()}&day={DAY}")
        assert answer.status_code == 303 and answer.headers["location"] == f"/review?day={DAY}"

    def page(self, day: str = DAY) -> Any:
        return self.web.get(f"/review?day={day}")

    def csrf(self) -> str:
        found = re.search(r'name="csrf" value="([^"]+)"', self.page().text)
        assert found, "the page carries no form token"
        return found.group(1)

    def decide(
        self,
        ids: list[str],
        do: str,
        *,
        csrf: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        form = {"csrf": csrf if csrf is not None else self.csrf(), "day": DAY, "do": do, "v": ids}
        return self.web.post("/review/decide", data=form, headers=headers or {})


@pytest.fixture
def review(conninfo: str, tmp_path: Path) -> Review:
    return Review(conninfo, tmp_path)


# --- switched off, signed out -----------------------------------------------------------------


def test_without_a_review_secret_the_page_does_not_exist(conninfo: str, tmp_path: Path) -> None:
    off = Review(conninfo, tmp_path, configured=False)
    off.idea("a-1")
    for method, path in (
        ("GET", "/review"),
        ("GET", f"/review/enter?t={off.link()}"),
        ("POST", "/review/decide"),
    ):
        answer = off.web.request(method, path)
        assert answer.status_code == 404, path
        assert "Review" not in answer.text and "sign" not in answer.text.lower()
    assert off.state("a-1") == {"instagram": "awaiting_approval", "tiktok": "awaiting_approval"}


def test_the_page_and_the_actions_need_the_session_cookie(review: Review) -> None:
    review.idea("a-1")
    page = review.page()
    assert page.status_code == 401 and "not signed in" in page.text.lower()
    assert "a-1" not in page.text  # nothing of the day leaks to someone signed out
    ids = list(review.ids("a-1").values())
    form = {"csrf": "x", "day": DAY, "do": "approve", "v": ids}
    assert review.web.post("/review/decide", data=form).status_code == 401
    assert review.state("a-1") == {"instagram": "awaiting_approval", "tiktok": "awaiting_approval"}


def test_a_signed_link_trades_for_a_locked_cookie_and_a_clean_address(review: Review) -> None:
    token = review.link()
    answer = review.web.get(f"/review/enter?t={token}&day={DAY}")
    assert answer.status_code == 303
    assert answer.headers["location"] == f"/review?day={DAY}"
    assert (
        token not in answer.headers["location"]
    )  # the link's token never stays in the address bar
    cookie = answer.headers["set-cookie"].lower()
    assert all(part in cookie for part in ("httponly", "secure", "samesite=lax", "path=/review"))
    assert "max-age=43200" in cookie  # twelve hours
    assert token not in answer.headers["set-cookie"]  # the cookie is a different, session token
    assert (
        answer.headers["cache-control"] == "no-store"
        and answer.headers["referrer-policy"]
        == "same-origin"  # not no-referrer: Chrome would send Origin: null
    )
    assert review.page().status_code == 200


def test_an_expired_link_says_so_and_how_to_get_a_new_one(review: Review) -> None:
    old = review.link(now=review.rig.clock.now() - timedelta(days=2))
    answer = review.web.get(f"/review/enter?t={old}")
    assert answer.status_code == 401 and "expired" in answer.text
    assert "dk review link" in answer.text and "Telegram" in answer.text
    assert "set-cookie" not in answer.headers
    assert old not in answer.text


def test_tampered_and_wrong_purpose_links_are_refused_without_a_cookie(review: Review) -> None:
    token = review.link()
    payload, signature = token.split(".")
    forged = f"{payload}.{signature[::-1]}"
    session_token = review.link(purpose=tokens.SESSION)  # genuine, but for a cookie, not a link
    for bad in (forged, session_token, "garbage", ""):
        answer = review.web.get(f"/review/enter?t={bad}")
        assert answer.status_code == 401 and "not valid" in answer.text, bad
        assert "set-cookie" not in answer.headers
    assert review.page().status_code == 401


def test_a_link_cannot_be_used_as_the_cookie_and_a_forged_cookie_is_refused(review: Review) -> None:
    review.web.cookies.set(
        "dk_review", review.link(), path="/review"
    )  # a LINK where a session belongs
    assert review.page().status_code == 401
    review.web.cookies.set("dk_review", "x.y", path="/review")
    assert review.page().status_code == 401
    old = review.link(purpose=tokens.SESSION, now=review.rig.clock.now() - timedelta(days=1))
    review.web.cookies.set("dk_review", old, path="/review")
    answer = review.page()
    assert answer.status_code == 401 and "expired" in answer.text


def test_a_session_ends_after_twelve_hours(review: Review) -> None:
    review.sign_in()
    assert review.page().status_code == 200
    review.rig.clock.advance(tokens.SESSION_TTL + timedelta(minutes=1))
    assert review.page().status_code == 401


def test_repeated_forgeries_close_the_door_even_for_a_good_link(review: Review) -> None:
    for _ in range(10):
        review.web.get("/review/enter?t=forged")
    answer = review.web.get(f"/review/enter?t={review.link()}")
    assert answer.status_code == 429 and "Too many" in answer.text
    review.rig.clock.advance(timedelta(minutes=11))
    assert review.web.get(f"/review/enter?t={review.link()}").status_code == 303


# --- what the page shows ----------------------------------------------------------------------


def test_the_day_is_listed_by_idea_with_media_captions_times_options_and_who_made_it(
    review: Review,
) -> None:
    review.idea("2026w47-mon-s1", hour=13)
    review.idea("2026w47-mon-s2", hour=18, platforms=("instagram", "tiktok", "youtube"))
    review.sign_in()
    html = review.page().text
    assert html.index("2026w47-mon-s1") < html.index("2026w47-mon-s2")  # earliest idea first
    assert html.count("<video") == 2  # one player per idea, not one per platform
    assert "https://media.example.test/tok/2026w47-mon-s1.mp4" in html
    assert "Instagram" in html and "Tiktok" in html and "Youtube" in html
    assert "13:00" in html and "18:00" in html and "Berlin" in html
    assert "privacy_level" in html and "public" in html and "Title 2026w47-mon-s2" in html
    assert "draft-key" in html and "by agent" in html  # who made it
    assert "Waiting for you" in html
    assert (
        "<b>2026w47-mon-s1</b>" not in html and "&lt;b&gt;2026w47-mon-s1&lt;/b&gt;" in html
    )  # escaped
    assert "<script" not in html.lower()
    assert "default-src 'none'" in review.page().headers["content-security-policy"]


def test_a_day_with_nothing_says_so_and_other_days_do_not_show_it(review: Review) -> None:
    review.idea("a-1")
    review.sign_in()
    assert "No posts from agents on this day" in review.page("2026-11-17").text
    assert "a-1" not in review.web.get("/review?day=junk").text  # an odd day falls back to today
    assert "a-1" in review.page(DAY).text


def test_a_post_whose_media_is_gone_is_named_not_hidden(conninfo: str, tmp_path: Path) -> None:
    review = Review(conninfo, tmp_path)
    review.idea("a-1")
    review.services = ReviewServices(
        core=review.services.core, secret=SECRET, base_url=BASE, tenant_id=TENANT, preview=None
    )
    app = create_app(review.rig.api, None, review.services)
    review.web = TestClient(app, base_url=BASE, follow_redirects=False)
    review.sign_in()
    html = review.page().text
    assert "No preview for <b>a-1.mp4</b>" in html and "<video" not in html


def test_the_same_idea_on_another_day_is_flagged(review: Review) -> None:
    review.idea("a-1")
    media = review.rig.upload("second.mp4", data=b"zzz" * 50)
    answer = review.rig.call(
        "POST",
        "/v1/posts/batch",
        json={
            "posts": [
                {
                    "post_key": "two-days",
                    "variants": [
                        {
                            "platform": "instagram",
                            "format": "reel",
                            "media": [media],
                            "slot": at(13),
                        },
                        {
                            "platform": "tiktok",
                            "format": "video",
                            "media": [media],
                            "slot": "2026-11-17T13:00:00+01:00",
                            "options": {"privacy_level": "public"},
                        },
                    ],
                }
            ]
        },
    )
    assert answer.json()["ok"] == 1
    review.sign_in()
    assert "also scheduled on 17 Nov" in review.page().text


# --- deciding: the same use cases as the Sheet ------------------------------------------------


def test_approving_one_variant_runs_the_real_approval_and_leaves_the_rest(review: Review) -> None:
    review.idea("a-1")
    review.sign_in()
    answer = review.decide([review.ids("a-1")["instagram"]], "approve")
    assert answer.status_code == 200 and "approved 1" in answer.text
    assert review.state("a-1") == {"instagram": "scheduled", "tiktok": "awaiting_approval"}
    with psycopg.connect(review.conninfo) as conn:
        row = conn.execute(
            """SELECT e.actor_kind, e.actor_name FROM publishing.variant_events e
               WHERE e.to_status = 'approved' AND e.variant_id = %s::uuid""",
            (review.ids("a-1")["instagram"],),
        ).fetchone()
    assert row == ("human", "review-page")  # the owner, from the page: never an agent key


def test_approving_an_idea_and_approving_all_for_the_day(review: Review) -> None:
    for key, hour in (("a-1", 13), ("a-2", 15), ("a-3", 17)):
        review.idea(key, hour=hour)
    review.sign_in()
    both = list(review.ids("a-1").values())
    assert "approved 2" in review.decide(both, "approve").text
    assert review.state("a-1") == {"instagram": "scheduled", "tiktok": "scheduled"}

    shown = daily_review.load_day(review.services.core, TENANT, DAY_DATE)
    waiting = list(shown.approvable)
    assert len(waiting) == 4  # a-1 is done; a-2 and a-3 still wait
    assert "approved 4" in review.decide(waiting, "approve").text
    assert all(set(review.state(k).values()) == {"scheduled"} for k in ("a-1", "a-2", "a-3"))


def test_rejecting_cancels_through_the_normal_path_for_a_variant_and_for_an_idea(
    review: Review,
) -> None:
    review.idea("a-1")
    review.idea("a-2", hour=15)
    review.sign_in()
    one = review.decide([review.ids("a-1")["tiktok"]], "reject")
    assert "rejected 1" in one.text
    assert review.state("a-1") == {"instagram": "awaiting_approval", "tiktok": "cancelled"}
    whole = review.decide(list(review.ids("a-2").values()), "reject")
    assert "rejected 2" in whole.text
    assert set(review.state("a-2").values()) == {"cancelled"}
    with psycopg.connect(review.conninfo) as conn:
        reasons = {
            r[0]
            for r in conn.execute(
                "SELECT reason FROM publishing.variant_events WHERE to_status = 'cancelled'"
            )
        }
    assert reasons == {"rejected in the review page"}
    # the agent's webhook story is untouched: it sees a normal cancellation of its own post
    assert review.rig.call("GET", "/v1/posts/a-2", "read").json()["summary"] == "cancelled"


def test_deciding_twice_is_harmless_and_says_nothing_was_waiting_any_more(review: Review) -> None:
    review.idea("a-1")
    review.sign_in()
    ids = list(review.ids("a-1").values())
    csrf = review.csrf()  # a page with nothing left to decide has no forms, so keep this one
    review.decide(ids, "approve", csrf=csrf)
    again = review.decide(ids, "approve", csrf=csrf)
    assert again.status_code == 200 and "2 no longer waiting, left alone" in again.text
    assert set(review.state("a-1").values()) == {"scheduled"}
    other_way = review.decide(
        ids, "reject", csrf=csrf
    )  # a scheduled post is not the page's to cancel
    assert "Done: 2 no longer waiting, left alone." in other_way.text
    assert set(review.state("a-1").values()) == {"scheduled"}


def test_a_slot_that_has_passed_is_not_offered_and_is_refused_if_sent(review: Review) -> None:
    review.idea("a-1")
    review.rig.clock.set(T0 + timedelta(days=2, hours=3))  # 15:00 Berlin: the 13:00 slot is gone
    review.sign_in()
    html = review.page().text
    assert "Its slot has passed" in html
    assert 'value="approve"' not in html  # no approve button anywhere
    answer = review.decide(list(review.ids("a-1").values()), "approve")
    assert "Done: nothing to do." in answer.text and "slot has passed" in answer.text
    assert set(review.state("a-1").values()) == {"awaiting_approval"}
    assert "rejected 2" in review.decide(list(review.ids("a-1").values()), "reject").text


def test_the_page_can_only_decide_agent_posts_never_the_sheets(review: Review) -> None:
    with review.rig.api.core.uow() as uow:
        post = uow.sync.upsert_post(TENANT, "sheet-post", "A Sheet post")
        account = next(a.id for a in uow.sync.accounts(TENANT) if a.platform == "instagram")
        human = Variant(
            id=str(uuid.uuid4()), tenant_id=TENANT, post_id=post, platform="instagram",
            account_id=account, publish_at=T0 + timedelta(days=2),
        )  # fmt: skip
        uow.variants.add(human, source_hash="h")
        uow.commit()
    review.idea("a-1")  # a day with an agent post, so the page has a form to send
    review.sign_in()
    answer = review.decide([human.id], "approve")
    assert "Done: 1 no longer waiting, left alone." in answer.text
    with psycopg.connect(review.conninfo) as conn:
        status = conn.execute(
            "SELECT status FROM publishing.variants WHERE id = %s::uuid", (human.id,)
        ).fetchone()
    assert status == ("draft",)


def test_an_agent_key_still_cannot_approve_and_is_no_session_for_the_page(review: Review) -> None:
    review.idea("a-1")
    assert review.rig.call("POST", "/v1/posts/a-1/approve", "draft").status_code == 403
    for scope in ("draft", "approve"):
        bearer = {"Authorization": f"Bearer {review.rig.keys[scope]}"}
        assert review.web.get("/review", headers=bearer).status_code == 401
        sent = review.web.post(
            "/review/decide",
            headers=bearer,
            data={"do": "approve", "csrf": "x", "v": list(review.ids("a-1").values())},
        )
        assert sent.status_code == 401
        assert (
            review.web.get(f"/review/enter?t={review.rig.keys[scope]}", headers=bearer).status_code
            == 401
        )
    assert review.state("a-1") == {"instagram": "awaiting_approval", "tiktok": "awaiting_approval"}


# --- forms are protected -----------------------------------------------------------------------


def test_a_post_needs_its_session_forms_token_and_a_same_site_origin(review: Review) -> None:
    review.idea("a-1")
    review.sign_in()
    ids = list(review.ids("a-1").values())
    untouched = {"instagram": "awaiting_approval", "tiktok": "awaiting_approval"}

    assert review.decide(ids, "approve", csrf="").status_code == 403
    assert review.decide(ids, "approve", csrf="forged").status_code == 403

    other = review.rig.clock.now()
    elsewhere = tokens.issue(SECRET, tokens.SESSION, other, tokens.SESSION_TTL)
    other_claims = tokens.check(SECRET, elsewhere, tokens.SESSION, other)[1]
    assert other_claims is not None
    stolen = tokens.csrf_token(SECRET, other_claims)  # a valid form token, but another session's
    assert review.decide(ids, "approve", csrf=stolen).status_code == 403

    cross_site = review.decide(ids, "approve", headers={"Origin": "https://evil.example.test"})
    assert cross_site.status_code == 400 and "did not come from this page" in cross_site.text
    assert review.decide(ids, "approve", headers={"Origin": "null"}).status_code == 400
    assert review.state("a-1") == untouched

    right_site = review.decide(ids, "approve", headers={"Origin": BASE})
    assert right_site.status_code == 200 and review.state("a-1")["instagram"] == "scheduled"


def test_a_post_that_is_not_a_form_or_names_nothing_changes_nothing(review: Review) -> None:
    review.idea("a-1")
    review.sign_in()
    as_json = review.web.post("/review/decide", json={"do": "approve"})
    assert as_json.status_code == 400
    empty = review.decide([], "approve")
    assert empty.status_code == 400 and "nothing to approve or reject" in empty.text
    odd = review.decide(list(review.ids("a-1").values()), "delete")
    assert odd.status_code == 400
    assert set(review.state("a-1").values()) == {"awaiting_approval"}


def test_no_token_or_cookie_value_ever_appears_in_a_page(review: Review) -> None:
    token = review.link()
    review.web.get(f"/review/enter?t={token}")
    cookie = review.web.cookies.get("dk_review") or ""
    assert cookie
    review.idea("a-1")
    for response in (
        review.page(),
        review.decide([], "approve"),
        review.decide(list(review.ids("a-1").values()), "approve"),
        review.web.get("/review/enter?t=forged"),
    ):
        assert token not in response.text and cookie not in response.text


# --- the morning digest -----------------------------------------------------------------------


def maker(review: Review) -> Any:
    def make(day: Any) -> str:
        return tokens.link_url(BASE, review.link(), day)

    return make


def test_the_digest_carries_the_review_link_when_agent_posts_wait_for_today(review: Review) -> None:
    review.idea("a-1")
    review.rig.clock.set(MORNING)
    telegram = FakeNotifier()
    assert send_digest(review.rig.api.core, telegram, review_link=maker(review))
    [text] = telegram.sent
    assert "2 posts from your agents wait for your OK" in text
    assert "Review today's posts" in text
    match = re.search(r'href="([^"]+)"', text)
    assert match
    link = match.group(1).replace("&amp;", "&")
    assert link.startswith(f"{BASE}/review/enter?t=") and link.endswith(f"&day={DAY}")
    # the very link in the message signs the owner in
    token = link.split("t=")[1].split("&")[0]
    assert review.web.get(f"/review/enter?t={token}&day={DAY}").status_code == 303


MORNING = T0 + timedelta(days=2) - timedelta(hours=5)  # 08:00 Berlin on DAY


def test_the_digest_has_no_review_line_when_nothing_waits_for_today(
    conninfo: str, tmp_path: Path
) -> None:
    review = Review(conninfo, tmp_path)
    review.rig.clock.set(MORNING)
    quiet = FakeNotifier()
    send_digest(review.rig.api.core, quiet, review_link=maker(review))
    assert "review" not in quiet.sent[0].lower() and "href" not in quiet.sent[0]


def test_the_digest_has_no_review_line_when_the_page_is_not_configured(
    conninfo: str, tmp_path: Path
) -> None:
    review = Review(conninfo, tmp_path, configured=False)
    review.idea("a-1")
    review.rig.clock.set(MORNING)
    off = FakeNotifier()
    send_digest(review.rig.api.core, off)  # drafts wait, but there is no way to make a link
    assert "review" not in off.sent[0].lower() and "href" not in off.sent[0]


def test_the_digest_counts_only_drafts_for_today_that_can_still_be_approved(review: Review) -> None:
    review.idea("a-1", hour=13)
    review.rig.clock.set(T0 + timedelta(days=2) - timedelta(hours=5))
    assert daily_review.waiting_on(review.rig.api.core, TENANT, DAY_DATE) == 2
    review.sign_in()
    review.decide(list(review.ids("a-1").values()), "approve")
    assert daily_review.waiting_on(review.rig.api.core, TENANT, DAY_DATE) == 0


# --- the Calendar's watch column ----------------------------------------------------------------


class Calendar:
    def __init__(self, review: Review, base: str) -> None:
        self.sheet = MemorySheet()
        self.services = SyncServices(
            core=review.rig.api.core,
            sheet=self.sheet,
            media=FakeMedia(),
            platforms=["instagram", "tiktok"],
            tenant_id=TENANT,
            review_base_url=base,
        )


def test_the_calendar_links_each_agent_post_to_its_day_on_the_review_page(review: Review) -> None:
    review.idea("a-1")
    calendar = Calendar(review, BASE)
    sync_sheet(calendar.services)
    watches = {e.platform: e.watch for e in calendar.sheet.calendar}
    expected = f"{BASE}/review?day={DAY}#i-a-1"
    assert watches == {"instagram": expected, "tiktok": expected}
    assert "t=" not in expected and SECRET not in expected  # a link in a Sheet holds no credential


def test_the_calendar_has_no_watch_link_when_the_page_is_off(review: Review) -> None:
    review.idea("a-1")
    calendar = Calendar(review, "")
    sync_sheet(calendar.services)
    assert [e.watch for e in calendar.sheet.calendar] == ["", ""]
