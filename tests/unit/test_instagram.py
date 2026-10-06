from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support import CAPS, T0
from tests.support.fake_graph import TOKEN, graph_error
from tests.support.fake_instagram import FakeInstagram

from dk_publishing.adapters.config.platforms import ConfigError, load_platforms
from dk_publishing.adapters.media.public import PublicMediaStore
from dk_publishing.adapters.platforms.instagram import InstagramPublisher
from dk_publishing.adapters.platforms.live import build_live_publisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_check import check_instagram, check_meta
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials, SectionTokenProvider
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG
from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)
from dk_publishing.domain.publishing import Rendition, VariantSnapshot

BASE = "https://media.example.test/m"


class Static:
    def token(self) -> str:
        return TOKEN


def build(
    fake: FakeInstagram | None = None,
    tmp_path: Path | None = None,
    *,
    public: bool = False,
    **kw: Any,
) -> tuple[InstagramPublisher, FakeInstagram]:
    fake = fake or FakeInstagram()
    graph = GraphClient(version="v25.0", tokens=Static(), transport=fake.transport())
    store = PublicMediaStore(tmp_path / "public", BASE) if public and tmp_path else None
    publisher = InstagramPublisher(
        account_id="IG1", capabilities=CAPS, graph=graph, version="v25.0", public=store,
        sleep=lambda s: None, **kw,
    )  # fmt: skip
    return publisher, fake


def snap(
    fmt: str = "reel",
    caption: str = "Kacchi tonight",
    media: tuple[str, ...] = ("clip.mp4",),
    **more: Any,
) -> VariantSnapshot:
    content = {
        "format": fmt,
        "caption": caption,
        "media": [{"name": n, "drive_file_id": "F", "md5": "m"} for n in media],
        **more,
    }
    return VariantSnapshot("v1", "dk", "instagram", "acct", T0, content)


def files(tmp_path: Path, name: str = "clip.mp4", data: bytes = b"REELBYTES") -> list[Rendition]:
    path = tmp_path / name
    path.write_bytes(data)
    return [Rendition("original", str(path), "sha")]


# --- validate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "field", "words"),
    [
        (snap("igtv"), "format", "must be one of: feed, carousel, reel, story"),
        (snap(caption="x" * 2201), "caption", "2,201 characters; Instagram allows 2,200"),
        (snap(caption="#a " * 31), "caption", "at most 30 hashtags"),
        (snap(media=()), "media", "needs exactly one file"),
        (snap(media=("a.mp4", "b.mp4")), "media", "needs exactly one file"),
        (snap(media=("a.jpg",)), "media", "'a.jpg' is not a reel file"),
        (snap("feed", media=("a.mp4",)), "media", "'a.mp4' is not a photo file"),
        (snap("feed", media=("a.png",)), "media", "'a.png' is not a photo file"),  # JPEG only
        (snap(cover_at_s=-1), "cover_at_s", "zero or more seconds"),
        (snap("feed", media=("a.jpg",)), "media", "public web address"),
    ],
)
def test_bad_posts_are_explained(snapshot: VariantSnapshot, field: str, words: str) -> None:
    problems = build()[0].validate(snapshot)
    assert any(p.field == field and words in p.message for p in problems), problems


def test_good_posts_pass(tmp_path: Path) -> None:
    publisher, _ = build(tmp_path=tmp_path, public=True)
    for snapshot in (
        snap(),
        snap(caption=""),  # a reel needs no words
        snap(media=("a.MOV",), share_to_feed=True, cover_at_s=2.5),
        snap("feed", media=("a.JPEG",)),
    ):
        assert publisher.validate(snapshot) == []


# --- a reel: resumable upload, no public address needed ---------------------------------------


def test_a_reel_is_uploaded_from_the_local_file_and_processed_before_publish(
    tmp_path: Path,
) -> None:
    fake = FakeInstagram()
    fake.polls_until_ready = 2
    publisher, _ = build(fake)
    handle = publisher.prepare(
        snap(share_to_feed=True, cover_at_s=2.5), files(tmp_path, data=b"REAL-REEL-BYTES")
    )

    create = fake.forms[0]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable"
    assert create["share_to_feed"] == "true" and create["thumb_offset"] == "2500"
    assert create["caption"] == "Kacchi tonight"

    [upload] = fake.uploads
    assert upload["bytes"] == b"REAL-REEL-BYTES" and upload["container"] == "C1"
    assert upload["headers"]["authorization"] == f"OAuth {TOKEN}"
    assert upload["headers"]["offset"] == "0" and upload["headers"]["file_size"] == "15"
    upload_request = next(r for r in fake.requests if r.url.host == "rupload.facebook.com")
    assert str(upload_request.url) == "https://rupload.facebook.com/ig-api-upload/v25.0/C1"
    assert handle["container_id"] == "C1" and handle["public_urls"] == []
    assert fake.media == []  # nothing is public yet


def test_share_to_feed_defaults_to_false_and_cover_is_optional(tmp_path: Path) -> None:
    publisher, fake = build()
    publisher.prepare(snap(), files(tmp_path))
    assert fake.forms[0]["share_to_feed"] == "false" and "thumb_offset" not in fake.forms[0]


def test_publishing_a_ready_container_goes_live_and_returns_the_link(tmp_path: Path) -> None:
    publisher, fake = build()
    live = publisher.publish(publisher.prepare(snap(), files(tmp_path)))
    assert live.external_id == "M2" and live.url == "https://www.instagram.com/reel/M2/"
    publish = next(r for r in fake.requests if r.url.path.endswith("media_publish"))
    assert fake.forms[-1]["creation_id"] == "C1" and TOKEN not in str(publish.url)


def test_the_token_never_travels_in_a_url_and_the_upload_header_is_not_logged_text(
    tmp_path: Path,
) -> None:
    publisher, fake = build()
    publisher.prepare(snap(), files(tmp_path))
    assert all(TOKEN not in str(r.url) for r in fake.requests if r.method == "POST")


def test_media_that_never_finishes_is_retried_after_the_wait(tmp_path: Path) -> None:
    fake = FakeInstagram()
    fake.polls_until_ready = 10_000
    publisher, _ = build(fake, ready_timeout=30.0, poll_every=10.0)
    with pytest.raises(Retryable, match="still processing"):
        publisher.prepare(snap(), files(tmp_path))


def test_media_instagram_cannot_process_is_rejected(tmp_path: Path) -> None:
    publisher, fake = build()
    real = fake.handle

    def error_status(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "status_code" in str(request.url):
            return httpx.Response(200, json={"id": "C1", "status_code": "ERROR"})
        return real(request)

    publisher._graph._client = httpx.Client(transport=httpx.MockTransport(error_status))
    with pytest.raises(Rejected, match="could not process the media"):
        publisher.prepare(snap(), files(tmp_path))


def test_a_reel_without_a_downloaded_file_is_rejected() -> None:
    with pytest.raises(Rejected, match="was not downloaded"):
        build()[0].prepare(snap(), [])


def test_an_upload_whose_answer_is_lost_is_uncertain_but_prepare_is_safe_to_repeat(
    tmp_path: Path,
) -> None:
    publisher, fake = build()
    real = fake.handle

    def lose_upload(request: httpx.Request) -> httpx.Response:
        if request.url.host == "rupload.facebook.com":
            raise httpx.ReadTimeout("lost")
        return real(request)

    publisher._graph._client = httpx.Client(transport=httpx.MockTransport(lose_upload))
    with pytest.raises(UnknownOutcome):
        publisher.prepare(snap(), files(tmp_path))
    assert fake.media == []  # preparing never posts, so the use case simply prepares again


# --- publish: nothing is sent until the container is ready ----------------------------------


def test_publishing_before_instagram_has_finished_sends_nothing_and_is_retryable(
    tmp_path: Path,
) -> None:
    fake = FakeInstagram()
    publisher, _ = build(fake)
    handle = publisher.prepare(snap(), files(tmp_path))
    fake.polls_until_ready = 5
    fake.containers["C1"]["polls"] = 0
    with pytest.raises(Retryable, match="not finished processing"):
        publisher.publish(handle)
    assert not [r for r in fake.requests if r.url.path.endswith("media_publish")]


def test_a_container_that_expired_is_rejected_not_published(tmp_path: Path) -> None:
    publisher, fake = build()
    handle = publisher.prepare(snap(), files(tmp_path))
    real = fake.handle

    def expired(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "status_code" in str(request.url):
            return httpx.Response(200, json={"id": "C1", "status_code": "EXPIRED"})
        return real(request)

    publisher._graph._client = httpx.Client(transport=httpx.MockTransport(expired))
    with pytest.raises(Rejected, match="no longer usable"):
        publisher.publish(handle)


def test_a_handle_without_a_container_is_rejected() -> None:
    with pytest.raises(Rejected, match="no container"):
        build()[0].publish({"kind": "reel"})


def test_a_link_lookup_failure_after_publishing_never_turns_success_into_failure(
    tmp_path: Path,
) -> None:
    publisher, fake = build()
    handle = publisher.prepare(snap(), files(tmp_path))
    real = fake.handle

    def break_permalink(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "permalink" in str(request.url):
            return httpx.Response(500, json={})
        return real(request)

    publisher._graph._client = httpx.Client(transport=httpx.MockTransport(break_permalink))
    live = publisher.publish(handle)
    assert live.external_id == "M2" and live.url is None


# --- photos: Instagram fetches them from a public address ------------------------------------


def test_a_photo_gets_a_public_link_that_is_removed_once_published(tmp_path: Path) -> None:
    publisher, fake = build(tmp_path=tmp_path, public=True)
    handle = publisher.prepare(snap("feed", media=("p.jpg",)), files(tmp_path, "p.jpg", b"JPEG"))
    url = handle["public_urls"][0]
    assert fake.forms[0]["image_url"] == url and url.startswith(BASE + "/")
    assert "upload_type" not in fake.forms[0] and fake.uploads == []
    assert len(list((tmp_path / "public").iterdir())) == 1

    publisher.publish(handle)
    assert list((tmp_path / "public").iterdir()) == []


def test_a_photo_that_cannot_be_prepared_leaves_no_public_link(tmp_path: Path) -> None:
    publisher, fake = build(tmp_path=tmp_path, public=True)
    fake.fail_next(graph_error(400, 100, "(#100) The image could not be fetched"), only="POST")
    with pytest.raises(Rejected):
        publisher.prepare(snap("feed", media=("p.jpg",)), files(tmp_path, "p.jpg"))
    assert list((tmp_path / "public").iterdir()) == []


# --- the daily allowance --------------------------------------------------------------------------


def test_at_the_daily_limit_the_post_waits_instead_of_being_attempted(tmp_path: Path) -> None:
    fake = FakeInstagram()
    fake.quota = (100, 100)
    publisher, _ = build(fake)
    with pytest.raises(RateLimited) as caught:
        publisher.prepare(snap(), files(tmp_path))
    assert caught.value.retry_after == timedelta(hours=1)
    assert fake.forms == []  # no container was made


def test_an_unreadable_allowance_does_not_block_posting(tmp_path: Path) -> None:
    fake = FakeInstagram()
    fake.quota_readable = False
    publisher, _ = build(fake)
    assert publisher.prepare(snap(), files(tmp_path))["container_id"] == "C1"


# --- never posting twice ----------------------------------------------------------------------


def test_a_lost_answer_to_publish_is_uncertain_and_the_post_is_then_found_not_reposted(
    tmp_path: Path,
) -> None:
    publisher, fake = build()
    handle = publisher.prepare(snap(), files(tmp_path))
    original = fake._post

    def post_then_lose(path: str, form: dict[str, str]) -> httpx.Response:
        response = original(path, form)
        if path.endswith("media_publish"):
            raise httpx.ReadTimeout("lost")
        return response

    fake._post = post_then_lose  # type: ignore[method-assign]
    with pytest.raises(UnknownOutcome):
        publisher.publish(handle)
    assert len(fake.media) == 1

    found = publisher.find_live(snap(), handle)
    assert found is not None and found.url == "https://www.instagram.com/reel/M2/"
    assert len(fake.media) == 1


def test_find_live_says_not_live_when_nothing_matches_and_recognises_a_published_container(
    tmp_path: Path,
) -> None:
    publisher, fake = build()
    handle = publisher.prepare(snap(), files(tmp_path))
    assert publisher.find_live(snap(), handle) is None
    publisher.publish(handle)
    fake.media.clear()  # published, but not in the list yet
    found = publisher.find_live(snap(), handle)
    assert found is not None and found.external_id == "C1"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (graph_error(400, 190, "expired", 463), AuthFailed),
        (httpx.Response(500, json={}), Retryable),
    ],
)
def test_when_instagram_cannot_answer_find_live_raises(
    outcome: Any, expected: type[Exception]
) -> None:
    publisher, fake = build()
    fake.fail_next(outcome)
    with pytest.raises(expected):
        publisher.find_live(snap(), None)


# --- going live: the Page token also serves Instagram -------------------------------------------


def credentials(tmp_path: Path, instagram_token: str = "", **instagram: object) -> MetaCredentials:
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "facebook": {"page_id": "PAGE", "access_token": TOKEN},
                "instagram": {"account_id": "IG1", "access_token": instagram_token, **instagram},
            }
        )
    )
    return MetaCredentials(path)


def test_an_empty_instagram_token_falls_back_to_the_page_token(tmp_path: Path) -> None:
    creds = credentials(tmp_path)
    assert SectionTokenProvider(creds, "instagram", fallback="facebook").token() == TOKEN
    with pytest.raises(AuthFailed):
        SectionTokenProvider(creds, "instagram").token()  # without a fallback it is empty


def test_an_instagram_token_of_its_own_wins_over_the_fallback(tmp_path: Path) -> None:
    folder = tmp_path / "own"
    folder.mkdir()
    own = credentials(folder, "OWN")
    assert SectionTokenProvider(own, "instagram", fallback="facebook").token() == "OWN"


def test_instagram_goes_live_from_the_one_credentials_file(tmp_path: Path) -> None:
    creds = credentials(tmp_path)
    publisher = build_live_publisher(
        "instagram",
        load_platforms(DEFAULT_PLATFORMS_CONFIG)["instagram"],
        {"META_CREDENTIALS_FILE": str(creds.path)},
        FakeInstagram().transport(),
    )
    assert isinstance(publisher, InstagramPublisher)
    assert any(
        "public web address" in p.message
        for p in publisher.validate(snap("feed", media=("a.jpg",)))
    )
    assert publisher.validate(snap()) == []  # reels need none


def test_a_file_without_an_instagram_account_id_is_refused(tmp_path: Path) -> None:
    creds = credentials(tmp_path, account_id="")
    with pytest.raises(ConfigError, match=r"instagram\.account_id is empty"):
        build_live_publisher(
            "instagram",
            load_platforms(DEFAULT_PLATFORMS_CONFIG)["instagram"],
            {"META_CREDENTIALS_FILE": str(creds.path)},
        )


# --- `dk meta check` for Instagram ----------------------------------------------------------------


def test_the_check_confirms_the_account_and_the_publishing_allowance(tmp_path: Path) -> None:
    report = check_instagram(credentials(tmp_path), transport=FakeInstagram().transport())
    text = "\n".join(report.lines)
    assert report.problems == [] and "Instagram @dhakakacchi (id IG1)" in text
    assert "0 of 100 API posts used" in text


def test_a_token_without_publishing_permission_is_called_out(tmp_path: Path) -> None:
    fake = FakeInstagram()
    fake.quota_readable = False
    report = check_instagram(credentials(tmp_path), transport=fake.transport())
    assert any(
        "cannot publish to Instagram" in p and "instagram_content_publish" in p
        for p in report.problems
    )


def test_a_token_that_cannot_see_the_account_explains_what_is_needed(tmp_path: Path) -> None:
    creds = credentials(tmp_path, "garbage")
    report = check_instagram(creds, transport=FakeInstagram().transport())
    assert any(
        "Instagram refused the token" in p and "professional account linked" in p
        for p in report.problems
    )


def test_missing_pieces_are_named(tmp_path: Path) -> None:
    assert check_instagram(credentials(tmp_path, account_id="")).problems == [
        "instagram.account_id is empty in the credentials file"
    ]
    path = tmp_path / "bare.json"
    path.write_text(
        json.dumps(
            {
                "facebook": {"page_id": "P", "access_token": ""},
                "instagram": {"account_id": "IG1", "access_token": ""},
            }
        )
    )
    assert any(
        "no Instagram token yet" in line for line in check_instagram(MetaCredentials(path)).lines
    )


def test_the_whole_check_reports_instagram_even_without_a_threads_section(tmp_path: Path) -> None:
    """An older credentials file has no threads section; that must not hide the Instagram result."""
    from tests.support.fake_graph import FakeGraph

    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "facebook": {"page_id": "PAGE", "access_token": TOKEN},
                "instagram": {"account_id": "IG1", "access_token": ""},
            }
        )
    )
    # The generic fake knows the Page but not the Instagram account, so Instagram reports a problem.
    report = check_meta(
        MetaCredentials(path), load_platforms(DEFAULT_PLATFORMS_CONFIG), FakeGraph().transport()
    )
    assert any("Instagram" in problem for problem in report.problems)


# --- a reel by public link: Instagram fetches it, nothing is uploaded ----------------------------


def test_with_a_public_address_a_reel_is_fetched_by_link_and_nothing_is_uploaded(
    tmp_path: Path,
) -> None:
    publisher, fake = build(tmp_path=tmp_path, public=True)
    handle = publisher.prepare(snap(share_to_feed=True), files(tmp_path))

    create = fake.forms[0]
    assert create["media_type"] == "REELS" and "upload_type" not in create
    assert create["video_url"].startswith(f"{BASE}/") and create["video_url"].endswith("/clip.mp4")
    assert fake.uploads == [] and not any(
        r.url.host == "rupload.facebook.com" for r in fake.requests
    )
    assert handle["public_urls"] == [create["video_url"]]


def test_the_link_is_removed_once_the_reel_is_live(tmp_path: Path) -> None:
    publisher, _ = build(tmp_path=tmp_path, public=True)
    handle = publisher.prepare(snap(), files(tmp_path))
    folders = list((tmp_path / "public").iterdir())
    assert len(folders) == 1
    publisher.publish(handle)
    assert list((tmp_path / "public").iterdir()) == []  # nobody can fetch the file any more


def test_a_reel_whose_link_instagram_cannot_fetch_leaves_no_public_link_behind(
    tmp_path: Path,
) -> None:
    fake = FakeInstagram()
    fake.fail_next(
        httpx.Response(400, json={"error": {"message": "cannot fetch", "code": 9004}}), only="POST"
    )
    publisher, _ = build(fake, tmp_path=tmp_path, public=True)
    with pytest.raises(PublishingError):
        publisher.prepare(snap(), files(tmp_path))
    assert list((tmp_path / "public").iterdir()) == []


# --- carousels and stories ------------------------------------------------------------------------


def photos(tmp_path: Path, n: int) -> list[Rendition]:
    out = []
    for i in range(n):
        path = tmp_path / f"{i + 1:02d}.jpg"
        path.write_bytes(b"JPEG" + bytes([i]))
        out.append(Rendition("original", str(path), f"sha{i}"))
    return out


def album(n: int = 4, **more: Any) -> VariantSnapshot:
    names = tuple(f"{i + 1:02d}.jpg" for i in range(n))
    return snap("carousel", media=names, **more)


def test_a_carousel_is_one_container_per_photo_plus_one_that_holds_them(tmp_path: Path) -> None:
    publisher, fake = build(tmp_path=tmp_path, public=True)
    handle = publisher.prepare(album(3, caption="Kacchi vs biryani"), photos(tmp_path, 3))

    children, parent = fake.forms[:3], fake.forms[3]
    assert all(
        c["is_carousel_item"] == "true" and c["image_url"].startswith(BASE) for c in children
    )
    assert parent["media_type"] == "CAROUSEL" and parent["caption"] == "Kacchi vs biryani"
    assert parent["children"] == "C1,C2,C3" and handle["container_id"] == "C4"
    assert len(handle["public_urls"]) == 3 and fake.uploads == []  # all fetched by link

    live = publisher.publish(handle)
    assert live.url == f"https://www.instagram.com/reel/{live.external_id}/"
    assert list((tmp_path / "public").iterdir()) == []  # every link removed after publishing
    assert publisher.find_live(album(3, caption="Kacchi vs biryani"), handle) is not None


@pytest.mark.parametrize(
    ("snapshot", "words"),
    [
        (album(1), "needs 2 to 10 photos, not 1"),
        (album(11), "needs 2 to 10 photos, not 11"),
        (snap("carousel", media=("a.jpg", "b.mp4")), "'b.mp4' is not a photo"),
    ],
)
def test_a_carousel_must_be_two_to_ten_photos(
    tmp_path: Path, snapshot: VariantSnapshot, words: str
) -> None:
    publisher, _ = build(tmp_path=tmp_path, public=True)
    assert any(words in p.message for p in publisher.validate(snapshot))


def test_a_carousel_cannot_be_made_without_a_public_address() -> None:
    problems = build()[0].validate(album(3))
    assert any("public web address" in p.message for p in problems)


def test_a_carousel_that_fails_part_way_leaves_no_public_link_behind(tmp_path: Path) -> None:
    fake = FakeInstagram()
    publisher, _ = build(fake, tmp_path=tmp_path, public=True)
    fake.fail_next(httpx.Response(400, json={"error": {"message": "x", "code": 9004}}), only="POST")
    with pytest.raises(PublishingError):
        publisher.prepare(album(3), photos(tmp_path, 3))
    assert list((tmp_path / "public").iterdir()) == []


def test_a_story_is_one_photo_or_video_and_has_no_caption(tmp_path: Path) -> None:
    publisher, fake = build(tmp_path=tmp_path, public=True)
    handle = publisher.prepare(
        snap("story", media=("01.jpg",), caption="ignored"), photos(tmp_path, 1)
    )
    form = fake.forms[0]
    assert form["media_type"] == "STORIES" and form["image_url"].startswith(BASE)
    assert "caption" not in form and "video_url" not in form

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"VID")
    video_story = snap("story", media=("clip.mp4",))
    publisher.prepare(video_story, [Rendition("original", str(clip), "s")])
    assert fake.forms[1]["media_type"] == "STORIES" and "video_url" in fake.forms[1]

    live = publisher.publish(handle)
    assert live.external_id and live.url is None  # stories have no permalink to show
    assert fake.media == [] and len(fake.stories) == 1


def test_a_story_that_was_published_is_recognised_by_its_container_not_by_a_caption(
    tmp_path: Path,
) -> None:
    publisher, _ = build(tmp_path=tmp_path, public=True)
    snapshot = snap("story", media=("01.jpg",))
    handle = publisher.prepare(snapshot, photos(tmp_path, 1))
    assert publisher.find_live(snapshot, handle) is None
    publisher.publish(handle)
    assert publisher.find_live(snapshot, handle) is not None


@pytest.mark.parametrize(
    ("media", "words"),
    [
        ((), "exactly one file"),
        (("a.jpg", "b.jpg"), "exactly one file"),
        (("a.gif",), "not a story file"),
    ],
)
def test_a_story_needs_exactly_one_photo_or_video(
    tmp_path: Path, media: tuple[str, ...], words: str
) -> None:
    publisher, _ = build(tmp_path=tmp_path, public=True)
    assert any(words in p.message for p in publisher.validate(snap("story", media=media)))
