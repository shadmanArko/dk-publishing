from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support import CAPS, T0
from tests.support.fake_graph import TOKEN, FakeGraph, graph_error

from dk_publishing.adapters.platforms import facebook
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.domain.errors import AuthFailed, Rejected, Retryable
from dk_publishing.domain.publishing import Rendition, VariantSnapshot


class Static:
    def token(self) -> str:
        return TOKEN


def build(fake: FakeGraph | None = None) -> tuple[FacebookPublisher, FakeGraph]:
    fake = fake or FakeGraph()
    graph = GraphClient(version="v25.0", tokens=Static(), transport=fake.transport())
    return FacebookPublisher(page_id="PAGE", capabilities=CAPS, graph=graph), fake


def snap(
    fmt: str = "post", caption: str = "Kacchi tonight", media: tuple[str, ...] = (), **more: Any
) -> VariantSnapshot:
    content = {
        "format": fmt,
        "caption": caption,
        "media": [{"name": n, "drive_file_id": f"id-{n}", "md5": "m"} for n in media],
        **more,
    }
    return VariantSnapshot("v1", "dk", "facebook", "acct", T0, content)


def rendition(tmp_path: Path, name: str = "eid.mp4", data: bytes = b"\x00video-bytes") -> Rendition:
    path = tmp_path / name
    path.write_bytes(data)
    return Rendition("original", str(path), "sha")


# --- validate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "field", "words"),
    [
        (snap("reel", media=("a.mp4",)), "format", "reels are not supported yet"),
        (snap("story"), "format", "must be one of: post, photo, video"),
        (snap(""), "format", "must be one of"),
        (snap("post", caption="  "), "caption", "needs a caption"),
        (
            snap("post", caption="x" * 70_000),
            "caption",
            "70,000 characters; Facebook allows 63,206",
        ),
        (snap("post", media=("a.jpg",)), "media", "cannot have media"),
        (snap("video"), "media", "needs a video file"),
        (snap("video", media=("a.mp4", "b.mp4")), "media", "Only one video"),
        (snap("video", media=("a.png",)), "media", "'a.png' is not a video file"),
        (snap("photo", media=("a.mp4",)), "media", "'a.mp4' is not a photo file"),
        (snap("photo"), "media", "needs a photo file"),
        (
            snap("video", media=("a.mp4",), link="https://x"),
            "link",
            "only be attached to a text post",
        ),
    ],
)
def test_bad_posts_are_explained_in_plain_words(
    snapshot: VariantSnapshot, field: str, words: str
) -> None:
    publisher, _ = build()
    problems = publisher.validate(snapshot)
    assert any(p.field == field and words in p.message for p in problems), problems


@pytest.mark.parametrize(
    "snapshot",
    [
        snap("post"),
        snap("post", link="https://dhakakacchi.de"),
        snap("video", caption="", media=("EID.MOV",)),
        snap("video", media=("a.mp4",)),
        snap("photo", media=("a.JPG",)),
    ],
)
def test_good_posts_pass(snapshot: VariantSnapshot) -> None:
    assert build()[0].validate(snapshot) == []


# --- prepare ----------------------------------------------------------------------------------


def test_prepare_checks_the_files_and_records_what_publish_needs(tmp_path: Path) -> None:
    publisher, fake = build()
    handle = publisher.prepare(snap("video", media=("eid.mp4",)), [rendition(tmp_path)])
    assert handle["kind"] == "video" and handle["page_id"] == "PAGE"
    assert handle["files"] == [str(tmp_path / "eid.mp4")] and handle["message"] == "Kacchi tonight"
    assert fake.requests == []  # preparing never talks to Meta


def test_a_video_post_without_a_downloaded_file_is_rejected() -> None:
    with pytest.raises(Rejected, match="was not downloaded"):
        build()[0].prepare(snap("video", media=("eid.mp4",)), [])


def test_a_file_that_vanished_is_rejected_by_name(tmp_path: Path) -> None:
    gone = Rendition("original", str(tmp_path / "gone.mp4"), "sha")
    with pytest.raises(Rejected, match=r"gone\.mp4 is missing"):
        build()[0].prepare(snap("video", media=("gone.mp4",)), [gone])


def test_an_oversized_video_is_rejected_before_any_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(facebook, "MAX_VIDEO_BYTES", 5)
    with pytest.raises(Rejected, match=r"eid.mp4 is \d+ MB; Facebook allows \d+ MB"):
        build()[0].prepare(snap("video", media=("eid.mp4",)), [rendition(tmp_path)])


# --- publish ----------------------------------------------------------------------------------


def test_a_text_post_goes_to_the_feed_and_returns_its_permalink() -> None:
    publisher, fake = build()
    live = publisher.publish(publisher.prepare(snap("post"), []))
    assert live.external_id == "PAGE_1" and live.url == "https://www.facebook.com/PAGE/posts/1"
    [post] = [r for r in fake.requests if r.method == "POST"]
    assert post.url.path == "/v25.0/PAGE/feed" and "message=Kacchi+tonight" in post.content.decode()
    assert fake.posted()[0]["message"] == "Kacchi tonight"


def test_a_link_is_sent_with_a_text_post() -> None:
    publisher, fake = build()
    publisher.publish(publisher.prepare(snap("post", link="https://dhakakacchi.de"), []))
    assert "link=https%3A%2F%2Fdhakakacchi.de" in fake.requests[0].content.decode()


def test_a_video_is_uploaded_to_the_video_host_with_its_bytes_and_caption(tmp_path: Path) -> None:
    publisher, fake = build()
    handle = publisher.prepare(
        snap("video", media=("eid.mp4",)), [rendition(tmp_path, data=b"REALBYTES")]
    )
    live = publisher.publish(handle)

    upload = fake.requests[0]
    assert upload.url.host == "graph-video.facebook.com" and upload.url.path == "/v25.0/PAGE/videos"
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b"REALBYTES" in upload.content
    assert fake.forms[0]["description"] == "Kacchi tonight" and fake.forms[0]["published"] == "true"
    assert fake.forms[0]["access_token"] == TOKEN
    assert live.external_id == "vid1" and live.url == "https://www.facebook.com/PAGE/videos/1"


def test_a_photo_uses_the_post_id_facebook_returns(tmp_path: Path) -> None:
    publisher, fake = build()
    handle = publisher.prepare(
        snap("photo", media=("p.jpg",)), [rendition(tmp_path, "p.jpg", b"JPEG")]
    )
    live = publisher.publish(handle)
    assert (
        fake.requests[0].url.path == "/v25.0/PAGE/photos"
        and fake.forms[0]["caption"] == "Kacchi tonight"
    )
    assert live.external_id == "PAGE_1"


def test_a_post_that_went_live_is_never_reported_failed_because_the_link_lookup_failed() -> None:
    publisher, fake = build()
    handle = publisher.prepare(snap("post"), [])
    real = fake.handle

    def break_permalink(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(500, json={})
        return real(request)

    publisher = FacebookPublisher(
        page_id="PAGE",
        capabilities=CAPS,
        graph=GraphClient(
            version="v25.0", tokens=Static(), transport=httpx.MockTransport(break_permalink)
        ),
    )
    live = publisher.publish(handle)  # must not raise
    assert live.external_id == "PAGE_1" and live.url == "https://www.facebook.com/PAGE_1"


def test_an_incomplete_handle_is_rejected_not_guessed_at() -> None:
    with pytest.raises(Rejected, match="missing what it needs"):
        build()[0].publish({"kind": "video", "files": []})


# --- find_live: the reconciliation read -------------------------------------------------------


def post_text(publisher: FacebookPublisher, text: str = "Kacchi tonight") -> None:
    publisher.publish(publisher.prepare(snap("post", caption=text), []))


def test_find_live_matches_the_caption_on_the_pages_feed() -> None:
    publisher, _ = build()
    post_text(publisher)
    found = publisher.find_live(snap("post"), None)
    assert found is not None and found.external_id == "PAGE_1"
    assert found.url == "https://www.facebook.com/PAGE/posts/1"


def test_find_live_says_not_live_when_nothing_matches() -> None:
    publisher, _ = build()
    assert publisher.find_live(snap("post"), None) is None
    post_text(publisher, "A different caption")
    assert publisher.find_live(snap("post"), None) is None


def test_an_older_post_with_the_same_caption_is_not_mistaken_for_this_one() -> None:
    fake = FakeGraph(created=datetime(2026, 11, 1, 12, 0, tzinfo=UTC))  # weeks before the slot
    publisher, _ = build(fake)
    post_text(publisher)
    assert publisher.find_live(snap("post"), None) is None


def test_a_post_made_a_little_before_the_slot_still_counts() -> None:
    fake = FakeGraph(created=T0 - timedelta(minutes=5))
    publisher, _ = build(fake)
    post_text(publisher)
    assert publisher.find_live(snap("post"), None) is not None


def test_find_live_reads_the_edge_that_matches_the_format(tmp_path: Path) -> None:
    publisher, fake = build()
    publisher.publish(
        publisher.prepare(snap("video", media=("e.mp4",)), [rendition(tmp_path, "e.mp4")])
    )
    found = publisher.find_live(snap("video", media=("e.mp4",)), None)
    assert found is not None and found.external_id == "vid1"
    assert fake.requests[-1].url.path == "/v25.0/PAGE/videos"

    publisher.publish(
        publisher.prepare(snap("photo", media=("p.jpg",)), [rendition(tmp_path, "p.jpg")])
    )
    assert publisher.find_live(snap("photo", media=("p.jpg",)), None) is not None
    assert "type=uploaded" in str(fake.requests[-1].url)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (graph_error(400, 190, "expired", 463), AuthFailed),
        (httpx.Response(500, json={}), Retryable),
        (httpx.ReadTimeout("slow"), Retryable),
    ],
)
def test_when_facebook_cannot_answer_find_live_raises_instead_of_claiming_not_live(
    outcome: httpx.Response | Exception, expected: type[Exception]
) -> None:
    publisher, fake = build()
    fake.fail_next(outcome)
    with pytest.raises(expected):
        publisher.find_live(snap("post"), None)


def test_confirming_a_post_reads_published_posts_never_the_feed() -> None:
    """Reading /feed needs an extra permission (error #10); posting to /feed does not."""
    publisher, fake = build()
    post_text(publisher)
    publisher.find_live(snap("post"), None)
    gets = [r.url.path for r in fake.requests if r.method == "GET"]
    assert "/v25.0/PAGE/published_posts" in gets
    assert not [p for p in gets if p.endswith("/feed")]
    assert [r.url.path for r in fake.requests if r.method == "POST"] == ["/v25.0/PAGE/feed"]


def test_a_token_that_may_not_read_the_feed_can_still_confirm_a_post() -> None:
    publisher, _ = build()
    post_text(publisher)
    # The fake refuses /feed reads exactly like the real Page does for this token.
    found = publisher.find_live(snap("post"), None)
    assert found is not None and found.external_id == "PAGE_1"
