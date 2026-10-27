"""LinkedIn is handed to a person on Telegram: its rules, its card and the image album."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support import CAPS, T0
from tests.support.fake_telegram import FakeNotifier
from tests.support.fake_tiktok import MemoryLog

from dk_publishing.adapters.notify.telegram import TelegramNotifier
from dk_publishing.adapters.platforms.linkedin import build_linkedin
from dk_publishing.application.ports import NotifyError
from dk_publishing.domain.errors import Rejected
from dk_publishing.domain.publishing import ASSISTED_PREFIX, Rendition, VariantSnapshot


def snap(**content: Any) -> VariantSnapshot:
    merged = {"caption": "Guten Tag aus der Küche", "media": [], **content}
    return VariantSnapshot("v1", "dk", "linkedin", "acct", T0, merged)


def build(notifier: FakeNotifier | None = None) -> Any:
    return build_linkedin(CAPS, notifier or FakeNotifier(), MemoryLog())


def files(tmp_path: Path, *names: str) -> list[Rendition]:
    out = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x" * 10)
        out.append(Rendition("original", str(path), "sha"))
    return out


def media(*names: str) -> list[dict[str, str]]:
    return [{"name": n} for n in names]


@pytest.mark.parametrize(
    ("override", "field", "words"),
    [
        ({"caption": "x" * 3001}, "caption", "allows 3000"),
        ({"caption": ""}, "caption", "needs a caption"),
        ({"media": media(*[f"{i}.jpg" for i in range(10)])}, "media", "at most 9"),
        ({"media": media("a.jpg", "b.mp4")}, "media", "exactly one video"),
        ({"media": media("a.pdf")}, "media", "not an image"),
        ({"format": "text", "media": media("a.jpg")}, "media", "no media"),
        ({"format": "image"}, "media", "needs its media"),
        ({"format": "video", "media": media("a.jpg")}, "media", "not a video"),
        ({"post_as": "company"}, "post_as", "Choose one of"),
    ],
)
def test_bad_rows_are_explained(override: dict[str, Any], field: str, words: str) -> None:
    problems = build().validate(snap(**override))
    assert any(p.field == field and words in p.message for p in problems), problems


@pytest.mark.parametrize(
    "content",
    [
        {},  # text only, no format (a Sheet row)
        {"format": "text"},
        {"media": media("a.jpg", "b.png", "c.gif")},
        {"format": "image", "media": media("a.JPG")},
        {"format": "video", "media": media("a.mp4"), "post_as": "profile"},
        {"caption": "", "media": media("a.mp4")},
    ],
)
def test_good_rows_pass(content: dict[str, Any]) -> None:
    assert build().validate(snap(**content)) == []


def test_a_text_post_needs_no_file_and_sends_only_the_card() -> None:
    telegram = FakeNotifier()
    publisher = build(telegram)
    handle = publisher.prepare(snap(), [])
    assert "Caption" in handle["card"] and "Post this on LinkedIn now" in handle["card"]
    assert "follows" not in handle["card"] and "follow in" not in handle["card"]
    live = publisher.publish(handle)
    assert live.external_id == f"{ASSISTED_PREFIX}v1"
    assert len(telegram.sent) == 1 and telegram.videos == [] and telegram.photos == []


def test_images_are_sent_as_one_album_after_the_card(tmp_path: Path) -> None:
    telegram = FakeNotifier()
    publisher = build(telegram)
    handle = publisher.prepare(
        snap(media=media("a.jpg", "b.jpg"), post_as="profile"), files(tmp_path, "a.jpg", "b.jpg")
    )
    assert "2 image(s) follow" in handle["card"] and "Post as: your own profile" in handle["card"]
    assert "first comment" in handle["card"]
    publisher.publish(handle)
    assert telegram.photos == [(["a.jpg", "b.jpg"], "🖼 a.jpg")] and telegram.videos == []


def test_a_video_is_sent_like_a_tiktok_video(tmp_path: Path) -> None:
    telegram = FakeNotifier()
    publisher = build(telegram)
    handle = publisher.prepare(snap(media=media("c.mp4")), files(tmp_path, "c.mp4"))
    assert "The video <i>c.mp4</i> follows" in handle["card"]
    publisher.publish(handle)
    assert telegram.videos == [("c.mp4", "🎬 c.mp4")] and telegram.photos == []


def test_an_oversized_image_is_refused_up_front(tmp_path: Path) -> None:
    big = tmp_path / "big.jpg"
    big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    with pytest.raises(Rejected, match="10 MB"):
        build().prepare(snap(media=media("big.jpg")), [Rendition("original", str(big), "sha")])


def test_a_file_that_is_missing_cannot_be_handed_over(tmp_path: Path) -> None:
    with pytest.raises(Rejected, match="is missing"):
        build().prepare(snap(), [Rendition("original", str(tmp_path / "gone.jpg"), "sha")])


def test_telegram_album_uses_a_media_group_and_one_photo_uses_send_photo(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramNotifier("123:SECRET", "42", transport=httpx.MockTransport(handler))
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    notifier.send_photos([str(a)], "one")
    notifier.send_photos([str(a), str(b)], "two")
    assert [r.url.path for r in seen] == [
        "/bot123:SECRET/sendPhoto",
        "/bot123:SECRET/sendMediaGroup",
    ]
    assert (
        b"AAA" in seen[1].content
        and b"BBB" in seen[1].content
        and b"attach://p1" in seen[1].content
    )


def test_telegram_photo_problems_are_explained(tmp_path: Path) -> None:
    notifier = TelegramNotifier(
        "t", "1", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    with pytest.raises(NotifyError, match="1 to 10"):
        notifier.send_photos([], "x")
    with pytest.raises(NotifyError, match="missing"):
        notifier.send_photos([str(tmp_path / "none.jpg")], "x")
