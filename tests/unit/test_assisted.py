"""Assisted publishing: a platform whose post is handed to a person on Telegram."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.contract.test_tiktok_contract import MemoryLog
from tests.support import CAPS, T0
from tests.support.fake_telegram import FakeNotifier

from dk_publishing.adapters.notify.telegram import MAX_CAPTION, TelegramNotifier
from dk_publishing.adapters.platforms import assisted
from dk_publishing.adapters.platforms.tiktok import build_tiktok
from dk_publishing.application.ports import NotifyError
from dk_publishing.domain.errors import Rejected, Retryable
from dk_publishing.domain.publishing import ASSISTED_PREFIX, Rendition, VariantSnapshot
from dk_publishing.domain.status import VariantStatus
from dk_publishing.domain.sync import SENT_TO_YOU, post_summary, row_status


def snap(**content: Any) -> VariantSnapshot:
    merged = {
        "caption": "Kacchi <tonight> & more",
        "privacy_level": "friends",
        "allow_comments": True,
        "media": [{"name": "clip.mp4"}],
        **content,
    }
    return VariantSnapshot("v1", "dk", "tiktok", "acct", T0, merged)


def video(tmp_path: Path, size: int = 10) -> list[Rendition]:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * size)
    return [Rendition("original", str(path), "sha")]


def build(notifier: FakeNotifier | None = None, log: MemoryLog | None = None) -> Any:
    return build_tiktok(CAPS, notifier or FakeNotifier(), log or MemoryLog())


# --- the rules a TikTok row must follow ----------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "field", "words"),
    [
        ({"privacy_level": ""}, "privacy_level", "TikTok has no default"),
        ({"privacy_level": "everyone"}, "privacy_level", "Choose who can see it"),
        ({"caption": "x" * 2201}, "caption", "keep it under 2200"),
        ({"media": []}, "media", "exactly one video"),
        ({"media": [{"name": "a.mp4"}, {"name": "b.mp4"}]}, "media", "exactly one video"),
        ({"media": [{"name": "photo.jpg"}]}, "media", "is not a video"),
    ],
)
def test_bad_rows_are_explained(override: dict[str, Any], field: str, words: str) -> None:
    problems = build().validate(snap(**override))
    assert any(p.field == field and words in p.message for p in problems), problems


def test_a_good_row_passes_whatever_the_privacy_level() -> None:
    for level in ("public", "friends", "followers", "only_me"):
        assert build().validate(snap(privacy_level=level)) == []


# --- the card the person receives ------------------------------------------------------------------


def test_the_card_has_the_caption_ready_to_copy_and_the_choices_to_make(tmp_path: Path) -> None:
    publisher = build()
    handle = publisher.prepare(snap(allow_duet=True, commercial_disclosure=True), video(tmp_path))
    card = handle["card"]
    assert "Post this on TikTok now" in card and "slot 14.11. 13:00" in card
    assert "<code>Kacchi &lt;tonight&gt; &amp; more</code>" in card  # copyable and escaped
    assert "Who can view: Friends" in card
    assert "Comments: on · Duet: on · Stitch: off" in card
    assert "commercial-content label" in card
    assert "follows in the next message" in card


def test_a_row_without_a_caption_still_gets_a_card(tmp_path: Path) -> None:
    card = build().prepare(snap(caption=""), video(tmp_path))["card"]
    assert "Caption" not in card and "Post this on TikTok now" in card


def test_preparing_sends_nothing_and_can_be_repeated(tmp_path: Path) -> None:
    telegram = FakeNotifier()
    publisher = build(telegram)
    publisher.prepare(snap(), video(tmp_path))
    publisher.prepare(snap(), video(tmp_path))
    assert telegram.sent == [] and telegram.videos == []


def test_without_a_downloaded_file_nothing_can_be_handed_over(tmp_path: Path) -> None:
    with pytest.raises(Rejected, match="not downloaded"):
        build().prepare(snap(), [])
    with pytest.raises(Rejected, match="is missing"):
        build().prepare(snap(), [Rendition("original", str(tmp_path / "gone.mp4"), "sha")])


# --- handing it over, exactly once -----------------------------------------------------------------


def test_publishing_sends_the_card_then_the_video_and_counts_as_sent_not_live(
    tmp_path: Path,
) -> None:
    telegram, log = FakeNotifier(), MemoryLog()
    publisher = build(telegram, log)
    handle = publisher.prepare(snap(), video(tmp_path))
    live = publisher.publish(handle)

    assert live.external_id == f"{ASSISTED_PREFIX}v1" and live.url is None
    assert len(telegram.sent) == 1 and telegram.videos == [("clip.mp4", "🎬 clip.mp4")]
    assert log.keys == {"assist:dk:v1"}


def test_a_lost_answer_is_settled_from_the_record_and_never_sends_a_second_card(
    tmp_path: Path,
) -> None:
    telegram, log = FakeNotifier(), MemoryLog()
    publisher = build(telegram, log)
    handle = publisher.prepare(snap(), video(tmp_path))
    assert publisher.find_live(snap(), handle) is None  # not sent yet
    publisher.publish(handle)
    found = publisher.find_live(snap(), handle)  # what reconcile asks after a crash
    assert found is not None and found.external_id == f"{ASSISTED_PREFIX}v1"
    assert len(telegram.sent) == 1


def test_a_telegram_outage_is_retryable_and_leaves_no_record(tmp_path: Path) -> None:
    telegram, log = FakeNotifier(), MemoryLog()
    telegram.down = True
    publisher = build(telegram, log)
    handle = publisher.prepare(snap(), video(tmp_path))
    with pytest.raises(Retryable, match="Telegram"):
        publisher.publish(handle)
    assert log.keys == set()
    assert publisher.find_live(snap(), handle) is None  # so the retry may send it


def test_a_video_over_telegrams_limit_is_described_not_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assisted, "ATTACH_LIMIT", 5)
    telegram = FakeNotifier()
    publisher = build(telegram)
    handle = publisher.prepare(snap(), video(tmp_path, size=50))
    assert (
        "over Telegram's 50 MB limit" in handle["card"] and "Drive media folder" in handle["card"]
    )
    publisher.publish(handle)
    assert len(telegram.sent) == 1 and telegram.videos == []


def test_an_incomplete_handle_is_rejected() -> None:
    with pytest.raises(Rejected, match="missing what it needs"):
        build().publish({"kind": "assisted"})


# --- what the Sheet says ----------------------------------------------------------------------------


def test_the_sheet_says_sent_to_you_instead_of_published() -> None:
    kwargs: dict[str, Any] = {
        "enabled": True,
        "problems": [],
        "last_reason": None,
        "edited_while_live": False,
    }
    assisted_row = row_status(
        variant_status=VariantStatus.PUBLISHED,
        external_url=None,
        external_id=f"{ASSISTED_PREFIX}v1",
        **kwargs,
    )
    assert (
        assisted_row[0] == SENT_TO_YOU and assisted_row[1] == "" and "Telegram" in assisted_row[2]
    )
    real = row_status(
        variant_status=VariantStatus.PUBLISHED,
        external_url="https://x/1",
        external_id="123",
        **kwargs,
    )
    assert real[:2] == ("published", "https://x/1")


def test_the_post_summary_counts_hand_overs_separately() -> None:
    assert post_summary(["published", SENT_TO_YOU]) == "1 of 2 live, 1 sent to you"


# --- Telegram: sending a video ------------------------------------------------------------------------


def test_the_video_is_uploaded_with_a_short_caption_and_no_url_leak(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramNotifier("123:SECRET", "42", transport=httpx.MockTransport(handler))
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"VIDEOBYTES")
    notifier.send_video(str(file), "c" * 2000)
    [request] = seen
    assert request.url.path == "/bot123:SECRET/sendVideo"
    body = request.content
    assert b"VIDEOBYTES" in body and b'name="chat_id"' in body
    assert b"c" * MAX_CAPTION in body and b"c" * (MAX_CAPTION + 1) not in body


def test_video_problems_become_one_explained_failure(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://api.telegram.org/bot123:SECRET/sendVideo")

    notifier = TelegramNotifier("123:SECRET", "42", transport=httpx.MockTransport(boom))
    with pytest.raises(NotifyError, match="missing"):
        notifier.send_video(str(tmp_path / "none.mp4"), "x")
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"x")
    with pytest.raises(NotifyError) as caught:
        notifier.send_video(str(file), "x")
    assert "SECRET" not in str(caught.value) and "ConnectError" in str(caught.value)


def test_a_video_over_the_limit_is_refused_before_uploading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dk_publishing.adapters.notify.telegram.MAX_VIDEO_BYTES", 0)
    file = tmp_path / "big.mp4"
    file.write_bytes(b"x")
    with pytest.raises(NotifyError, match="50 MB"):
        TelegramNotifier("t", "1").send_video(str(file), "x")


def test_telegram_refusing_a_video_is_reported(tmp_path: Path) -> None:
    reject = httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False, "description": "wrong file"})
    )
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"x")
    with pytest.raises(NotifyError, match="400: wrong file"):
        TelegramNotifier("t", "1", transport=reject).send_video(str(file), "x")
    html = httpx.MockTransport(lambda r: httpx.Response(502, text="<html>"))
    with pytest.raises(NotifyError, match="502"):
        TelegramNotifier("t", "1", transport=html).send_video(str(file), "x")
