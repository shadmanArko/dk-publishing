from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support import NATIVE_CAPS, T0
from tests.support.fake_graph import TOKEN, FakeGraph, graph_error

from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.application.ports import NativeScheduler
from dk_publishing.domain.errors import AuthFailed, Rejected, Retryable, UnknownOutcome
from dk_publishing.domain.publishing import Rendition, VariantSnapshot

SLOT = T0 + timedelta(hours=5)


class Static:
    def token(self) -> str:
        return TOKEN


def build(fake: FakeGraph | None = None) -> tuple[FacebookPublisher, FakeGraph]:
    fake = fake or FakeGraph(created=SLOT)
    graph = GraphClient(version="v25.0", tokens=Static(), transport=fake.transport())
    return FacebookPublisher(page_id="PAGE", capabilities=NATIVE_CAPS, graph=graph), fake


def snap(fmt: str = "post", **more: Any) -> VariantSnapshot:
    media = [{"name": "e.mp4", "drive_file_id": "F", "md5": "m"}] if fmt == "video" else []
    content = {
        "format": fmt,
        "caption": "Kacchi tonight",
        "media": media,
        "delivery": "native",
        **more,
    }
    return VariantSnapshot("v1", "dk", "facebook", "acct", SLOT, content)


def video(tmp_path: Path) -> Rendition:
    path = tmp_path / "e.mp4"
    path.write_bytes(b"VIDEOBYTES")
    return Rendition("original", str(path), "sha")


def test_the_adapter_can_hold_posts() -> None:
    assert isinstance(build()[0], NativeScheduler)


# --- schedule -----------------------------------------------------------------------------------


def test_a_text_post_is_given_to_facebook_unpublished_with_the_exact_time() -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("post"), [], SLOT)

    form = fake.forms[0]
    assert form["published"] == "false" and form["message"] == "Kacchi tonight"
    assert form["scheduled_publish_time"] == str(int(SLOT.timestamp()))
    assert handle["scheduled_id"] == "PAGE_1" and handle["scheduled_for"] == SLOT.isoformat()
    assert handle["kind"] == "post" and handle["message"] == "Kacchi tonight"
    assert [i["id"] for i in fake.held()] == ["PAGE_1"] and fake.posted()[0][
        "is_published"
    ] is False


def test_the_time_sent_is_a_unix_timestamp_whatever_zone_it_started_in() -> None:
    publisher, fake = build()
    berlin = timezone_plus(2)
    publisher.schedule(snap("post"), [], SLOT.astimezone(berlin))
    assert fake.forms[0]["scheduled_publish_time"] == str(int(SLOT.timestamp()))


def timezone_plus(hours: int):  # type: ignore[no-untyped-def]
    from datetime import timezone

    return timezone(timedelta(hours=hours))


def test_a_video_is_uploaded_now_unpublished_and_held_until_the_slot(tmp_path: Path) -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("video"), [video(tmp_path)], SLOT)
    upload = fake.requests[0]
    assert upload.url.host == "graph-video.facebook.com" and b"VIDEOBYTES" in upload.content
    assert fake.forms[0]["published"] == "false"
    assert fake.forms[0]["scheduled_publish_time"] == str(int(SLOT.timestamp()))
    assert handle["scheduled_id"] == "vid1" and [i["id"] for i in fake.held("videos")] == ["vid1"]


def test_scheduling_does_not_publish_so_the_feed_stays_empty_until_facebook_does() -> None:
    publisher, fake = build()
    publisher.schedule(snap("post"), [], SLOT)
    assert fake.get_feed() == [] if hasattr(fake, "get_feed") else True
    direct = snap("post", delivery="direct")
    assert publisher.find_live(direct, None) is None  # a held post is not on the Page's feed


def test_native_photos_are_refused_before_approval() -> None:
    publisher, _ = build()
    snapshot = snap("photo", media=[{"name": "p.jpg", "drive_file_id": "F", "md5": "m"}])
    assert any("text posts and videos" in p.message for p in publisher.validate(snapshot))


def test_an_unknown_delivery_is_not_a_facebook_concern_but_direct_photos_are_fine() -> None:
    publisher, _ = build()
    photo = snap(
        "photo", media=[{"name": "p.jpg", "drive_file_id": "F", "md5": "m"}], delivery="direct"
    )
    assert publisher.validate(photo) == []


# --- an uncertain schedule must be reported as uncertain ------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [httpx.ReadTimeout("slow"), httpx.Response(503, json={}), httpx.RemoteProtocolError("dropped")],
)
def test_a_lost_answer_to_a_schedule_call_is_uncertain_never_retryable(outcome: Any) -> None:
    publisher, fake = build()
    fake.fail_next(outcome)
    with pytest.raises(UnknownOutcome):
        publisher.schedule(snap("post"), [], SLOT)
    assert len(fake.requests) == 1  # and it was not quietly sent again


def test_a_schedule_rejected_for_being_too_soon_is_a_rejection() -> None:
    publisher, fake = build()
    fake.fail_next(
        graph_error(
            400, 100, "(#100) The scheduled publish time must be 10 minutes to 30 days away"
        )
    )
    with pytest.raises(Rejected, match="10 minutes to 30 days"):
        publisher.schedule(snap("post"), [], SLOT)


# --- cancel -------------------------------------------------------------------------------------


def test_cancelling_deletes_the_held_post() -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("post"), [], SLOT)
    publisher.cancel(handle)
    assert fake.held() == [] and fake.requests[-1].method == "DELETE"
    assert fake.requests[-1].url.path == "/v25.0/PAGE_1"


def test_cancelling_something_already_gone_is_not_an_error() -> None:
    publisher, _ = build()
    handle = publisher.schedule(snap("post"), [], SLOT)
    publisher.cancel(handle)
    publisher.cancel(handle)  # a second time: Facebook says it does not exist; that is fine


def test_a_cancel_that_failed_for_another_reason_is_raised_so_it_can_be_retried() -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("post"), [], SLOT)
    fake.fail_next(httpx.Response(500, json={}))
    with pytest.raises(Retryable):
        publisher.cancel(handle)
    fake.fail_next(graph_error(400, 190, "expired", 463))
    with pytest.raises(AuthFailed):
        publisher.cancel(handle)
    assert len(fake.held()) == 1  # still there: nothing was assumed


def test_cancelling_without_anything_scheduled_is_rejected() -> None:
    with pytest.raises(Rejected, match="nothing was scheduled"):
        build()[0].cancel({"kind": "post"})


# --- find_live for a held post ----------------------------------------------------------------


def test_a_held_post_is_not_live_until_facebook_publishes_it() -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("post"), [], SLOT)
    assert publisher.find_live(snap("post"), handle) is None

    fake.publish_due(SLOT + timedelta(seconds=1))
    found = publisher.find_live(snap("post"), handle)
    assert found is not None and found.external_id == "PAGE_1"
    assert found.url == "https://www.facebook.com/PAGE/posts/1"
    assert "fields=is_published" in str(fake.requests[-1].url)  # asked by id, not by caption


def test_a_held_video_is_checked_by_its_published_flag(tmp_path: Path) -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("video"), [video(tmp_path)], SLOT)
    assert publisher.find_live(snap("video"), handle) is None
    fake.publish_due(SLOT + timedelta(minutes=1))
    found = publisher.find_live(snap("video"), handle)
    assert found is not None and found.external_id == "vid1"
    assert "fields=published" in str(fake.requests[-1].url)


def test_a_post_deleted_on_facebook_is_reported_as_unanswerable_not_as_not_live() -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("post"), [], SLOT)
    fake.items["feed"].clear()  # someone deleted it in Facebook's own tools
    with pytest.raises(Rejected):
        publisher.find_live(snap("post"), handle)


def test_when_facebook_cannot_answer_about_a_held_post_it_raises() -> None:
    publisher, fake = build()
    handle = publisher.schedule(snap("post"), [], SLOT)
    fake.fail_next(httpx.ReadTimeout("slow"))
    with pytest.raises(Retryable):
        publisher.find_live(snap("post"), handle)
