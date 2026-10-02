from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support import CAPS, T0
from tests.support.fake_graph import graph_error
from tests.support.fake_threads import THREADS_TOKEN, FakeThreads

from dk_publishing.adapters.config.platforms import ConfigError, load_platforms
from dk_publishing.adapters.media.public import PublicMediaStore
from dk_publishing.adapters.platforms.live import build_live_publisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.meta_check import check_threads
from dk_publishing.adapters.platforms.meta_credentials import MetaCredentials
from dk_publishing.adapters.platforms.threads import HOST, ThreadsPublisher, _length
from dk_publishing.adapters.platforms.threads_token import (
    days_left,
    refresh_expiring_tokens,
    refresh_threads_token,
)
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG
from dk_publishing.domain.errors import AuthFailed, Rejected, Retryable, UnknownOutcome
from dk_publishing.domain.publishing import Rendition, VariantSnapshot

BASE = "https://media.example.test/m"


class Static:
    def token(self) -> str:
        return THREADS_TOKEN


def build(
    fake: FakeThreads | None = None,
    tmp_path: Path | None = None,
    *,
    public: bool = False,
    **kw: Any,
) -> tuple[ThreadsPublisher, FakeThreads, PublicMediaStore | None]:
    fake = fake or FakeThreads()
    graph = GraphClient(
        version="v1.0", tokens=Static(), transport=fake.transport(), host=HOST, video_host=HOST
    )
    store = PublicMediaStore(tmp_path / "public", BASE) if public and tmp_path else None
    sleeps: list[float] = []
    publisher = ThreadsPublisher(
        user_id="TH1", capabilities=CAPS, graph=graph, public=store, sleep=sleeps.append, **kw
    )
    publisher.sleeps = sleeps  # type: ignore[attr-defined]
    return publisher, fake, store


def snap(
    fmt: str = "text", caption: str = "Kacchi tonight", media: tuple[str, ...] = (), **more: Any
) -> VariantSnapshot:
    content = {
        "format": fmt,
        "caption": caption,
        "media": [{"name": n, "drive_file_id": "F", "md5": "m"} for n in media],
        **more,
    }
    return VariantSnapshot("v1", "dk", "threads", "acct", T0, content)


def rendition(tmp_path: Path, name: str = "clip.mp4") -> Rendition:
    path = tmp_path / name
    path.write_bytes(b"BYTES")
    return Rendition("original", str(path), "sha")


# --- validate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "field", "words"),
    [
        (snap("carousel"), "format", "carousels are not supported yet"),
        (snap("story"), "format", "must be one of: text, image, video"),
        (snap(caption="  "), "caption", "needs text"),
        (snap(caption="x" * 501), "caption", "501 characters; Threads allows 500"),
        (snap(reply_control="nobody"), "reply_control", "must be one of"),
        (snap(media=("a.jpg",)), "media", "cannot have media"),
        (snap("video"), "media", "needs exactly one video file"),
        (snap("image", media=("a.jpg", "b.jpg")), "media", "needs exactly one image file"),
        (snap("image", media=("a.mp4",)), "media", "not a image file"),
        (snap("video", media=("a.mp4",)), "media", "public web address"),  # no public store here
    ],
)
def test_bad_threads_are_explained(snapshot: VariantSnapshot, field: str, words: str) -> None:
    problems = build()[0].validate(snapshot)
    assert any(p.field == field and words in p.message for p in problems), problems


def test_good_threads_pass(tmp_path: Path) -> None:
    publisher = build(tmp_path=tmp_path, public=True)[0]
    for snapshot in (
        snap(),
        snap(reply_control="mentioned_only"),
        snap("video", caption="", media=("a.MOV",)),
        snap("image", media=("a.png",)),
    ):
        assert publisher.validate(snapshot) == []


def test_emoji_count_as_their_bytes_so_the_limit_is_honest() -> None:
    assert _length("abc") == 3 and _length("🍛") == 4 and _length("é") == 1
    problems = build()[0].validate(snap(caption="🍛" * 126))  # 126 x 4 = 504 > 500
    assert problems and "504 characters" in problems[0].message


# --- text posts -------------------------------------------------------------------------------


def test_a_text_post_is_a_container_then_a_publish_and_returns_its_link() -> None:
    publisher, fake, _ = build()
    handle = publisher.prepare(snap(), [])
    assert handle["container_id"] == "C1" and fake.posted() == []  # preparing posts nothing

    live = publisher.publish(handle)
    assert live.external_id == "T2" and live.url == "https://www.threads.net/@dk/post/T2"
    create, publish = [r for r in fake.requests if r.method == "POST"]
    assert (
        create.url.path == "/v1.0/TH1/threads" and publish.url.path == "/v1.0/TH1/threads_publish"
    )
    assert fake.forms[0]["media_type"] == "TEXT" and fake.forms[0]["text"] == "Kacchi tonight"
    assert fake.forms[1]["creation_id"] == "C1"
    assert all(r.url.host == "graph.threads.net" for r in fake.requests)


def test_reply_control_is_passed_through() -> None:
    publisher, fake, _ = build()
    publisher.prepare(snap(reply_control="accounts_you_follow"), [])
    assert fake.forms[0]["reply_control"] == "accounts_you_follow"


def test_the_token_is_sent_in_the_body_of_writes_never_in_a_url() -> None:
    publisher, fake, _ = build()
    publisher.publish(publisher.prepare(snap(), []))
    for request in fake.requests:
        if request.method == "POST":
            assert THREADS_TOKEN in request.content.decode() and THREADS_TOKEN not in str(
                request.url
            )


def test_a_permalink_failure_after_publishing_never_turns_success_into_failure() -> None:
    publisher, fake, _ = build()
    handle = publisher.prepare(snap(), [])
    real = fake.handle

    def break_permalink(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "permalink" in str(request.url):
            return httpx.Response(500, json={})
        return real(request)

    publisher = ThreadsPublisher(
        user_id="TH1",
        capabilities=CAPS,
        graph=GraphClient(
            version="v1.0",
            tokens=Static(),
            transport=httpx.MockTransport(break_permalink),
            host=HOST,
            video_host=HOST,
        ),
    )
    live = publisher.publish(handle)
    assert live.external_id == "T2" and live.url is None


def test_a_handle_without_a_container_is_rejected() -> None:
    with pytest.raises(Rejected, match="no container"):
        build()[0].publish({"kind": "text"})


# --- media ------------------------------------------------------------------------------------


def test_a_video_gets_a_public_link_and_waits_until_threads_has_processed_it(
    tmp_path: Path,
) -> None:
    fake = FakeThreads()
    fake.polls_until_ready = 3
    publisher, _, _ = build(fake, tmp_path, public=True)
    handle = publisher.prepare(snap("video", media=("clip.mp4",)), [rendition(tmp_path)])

    [url] = handle["public_urls"]
    assert url.startswith(BASE + "/") and url.endswith("/clip.mp4")
    assert fake.forms[0]["media_type"] == "VIDEO" and fake.forms[0]["video_url"] == url
    assert publisher.sleeps == [5.0, 5.0, 5.0]  # type: ignore[attr-defined]
    assert len(list((tmp_path / "public").iterdir())) == 1  # the link is live while it is needed


def test_publishing_removes_the_public_link(tmp_path: Path) -> None:
    publisher, fake, _ = build(tmp_path=tmp_path, public=True)
    handle = publisher.prepare(snap("image", media=("p.jpg",)), [rendition(tmp_path, "p.jpg")])
    assert fake.forms[0]["image_url"] == handle["public_urls"][0]
    publisher.publish(handle)
    assert list((tmp_path / "public").iterdir()) == []  # nobody can fetch the file any more


def test_media_that_never_finishes_processing_is_retried_and_leaves_no_public_link(
    tmp_path: Path,
) -> None:
    fake = FakeThreads()
    fake.polls_until_ready = 10_000
    publisher, _, _ = build(fake, tmp_path, public=True, ready_timeout=15.0)
    with pytest.raises(Retryable, match="still processing"):
        publisher.prepare(snap("video", media=("clip.mp4",)), [rendition(tmp_path)])
    assert list((tmp_path / "public").iterdir()) == []


def test_media_threads_cannot_process_is_rejected_and_leaves_no_public_link(tmp_path: Path) -> None:
    publisher, fake, _ = build(tmp_path=tmp_path, public=True)
    real = fake.handle

    def error_status(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "status" in str(request.url) and "/C1" in request.url.path:
            return httpx.Response(200, json={"id": "C1", "status": "ERROR"})
        return real(request)

    publisher._graph._client = httpx.Client(transport=httpx.MockTransport(error_status))
    with pytest.raises(Rejected, match="could not process the media"):
        publisher.prepare(snap("video", media=("clip.mp4",)), [rendition(tmp_path)])
    assert list((tmp_path / "public").iterdir()) == []


def test_publishing_before_threads_has_processed_the_media_sends_nothing_and_is_retryable(
    tmp_path: Path,
) -> None:
    fake = FakeThreads()
    publisher, _, _ = build(fake, tmp_path, public=True)
    handle = publisher.prepare(snap("video", media=("clip.mp4",)), [rendition(tmp_path)])
    fake.polls_until_ready = 10  # now it reports IN_PROGRESS again
    fake.containers["C1"]["polls"] = 0
    with pytest.raises(Retryable, match="not finished processing"):
        publisher.publish(handle)
    assert not [r for r in fake.requests if r.url.path.endswith("threads_publish")]


def test_media_needs_a_file_and_a_public_address() -> None:
    publisher, _, _ = build()
    with pytest.raises(Rejected, match="no public address"):
        publisher.prepare(snap("video", media=("clip.mp4",)), [])


# --- never posting twice ----------------------------------------------------------------------


def test_a_lost_answer_to_publish_is_uncertain_and_the_post_is_then_found_not_reposted() -> None:
    publisher, fake, _ = build()
    handle = publisher.prepare(snap(), [])
    fake.lose_next_response = False
    original = fake._post

    def post_then_lose(path: str, form: dict[str, str]) -> httpx.Response:
        response = original(path, form)
        if path.endswith("threads_publish"):
            raise httpx.ReadTimeout("lost")
        return response

    fake._post = post_then_lose  # type: ignore[method-assign]
    with pytest.raises(UnknownOutcome):
        publisher.publish(handle)
    assert len(fake.posted()) == 1  # Threads published it

    found = publisher.find_live(snap(), handle)
    assert found is not None and found.external_id == "T2"
    assert len(fake.posted()) == 1  # and nothing was posted again


def test_find_live_says_not_live_when_nothing_matches_or_it_is_old() -> None:
    publisher, _, _ = build()
    assert publisher.find_live(snap(), None) is None
    publisher.publish(publisher.prepare(snap(caption="Something else"), []))
    assert publisher.find_live(snap(), None) is None

    old, _, _ = build(FakeThreads(created=T0 - timedelta(days=2)))
    old.publish(old.prepare(snap(), []))
    assert old.find_live(snap(), None) is None  # same text, but weeks ago


def test_a_published_container_is_recognised_even_before_it_shows_in_the_list() -> None:
    publisher, fake, _ = build()
    handle = publisher.prepare(snap(), [])
    publisher.publish(handle)
    fake.threads.clear()  # not in the list yet
    found = publisher.find_live(snap(), handle)
    assert found is not None and found.external_id == "C1"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (graph_error(400, 190, "expired", 463), AuthFailed),
        (httpx.Response(500, json={}), Retryable),
        (httpx.ReadTimeout("slow"), Retryable),
    ],
)
def test_when_threads_cannot_answer_find_live_raises(
    outcome: Any, expected: type[Exception]
) -> None:
    publisher, fake, _ = build()
    fake.fail_next(outcome)
    with pytest.raises(expected):
        publisher.find_live(snap(), None)


# --- the public link store ------------------------------------------------------------------------


def test_each_file_gets_its_own_unguessable_link_and_the_original_is_untouched(
    tmp_path: Path,
) -> None:
    store = PublicMediaStore(tmp_path / "public", BASE)
    original = rendition(tmp_path, "my clip!.mp4")
    first, second = store.expose(original), store.expose(original)
    assert first != second and first.startswith(BASE + "/") and first.endswith("/my_clip_.mp4")
    token = first.split("/")[-2]
    assert len(token) >= 30 and (tmp_path / "public" / token / "my_clip_.mp4").exists()
    assert Path(original.path).read_bytes() == b"BYTES"


def test_revoking_removes_only_that_link(tmp_path: Path) -> None:
    store = PublicMediaStore(tmp_path / "public", BASE)
    a, b = store.expose(rendition(tmp_path, "a.mp4")), store.expose(rendition(tmp_path, "b.mp4"))
    store.revoke(a)
    assert [p.name for p in (tmp_path / "public").iterdir()] == [b.split("/")[-2]]
    store.revoke("https://x/y")  # too short to be one of ours: ignored
    store.revoke(f"{BASE}/../../etc/z")  # cannot escape the folder
    assert (tmp_path / "public" / b.split("/")[-2]).exists()


def test_links_nobody_revoked_are_purged_after_a_while(tmp_path: Path) -> None:
    store = PublicMediaStore(tmp_path / "public", BASE)
    store.expose(rendition(tmp_path))
    assert store.purge_older_than(timedelta(hours=2)) == 0
    later = datetime.now(UTC) + timedelta(hours=3)
    assert store.purge_older_than(timedelta(hours=2), now=later) == 1
    assert list((tmp_path / "public").iterdir()) == []
    assert PublicMediaStore(tmp_path / "missing", BASE).purge_older_than(timedelta(hours=1)) == 0


# --- going live ---------------------------------------------------------------------------------


def credentials(tmp_path: Path, **threads: object) -> MetaCredentials:
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps({"threads": {"user_id": "TH1", "access_token": THREADS_TOKEN, **threads}})
    )
    return MetaCredentials(path)


def settings() -> Any:
    return load_platforms(DEFAULT_PLATFORMS_CONFIG)["threads"]


def test_threads_goes_live_from_the_one_credentials_file(tmp_path: Path) -> None:
    creds = credentials(tmp_path)
    publisher = build_live_publisher(
        "threads", settings(), {"META_CREDENTIALS_FILE": str(creds.path)}, FakeThreads().transport()
    )
    assert isinstance(publisher, ThreadsPublisher)
    # No public web address here, so media posts are refused with a clear reason, text is fine.
    assert publisher.validate(snap()) == []
    assert any(
        "public web address" in p.message
        for p in publisher.validate(snap("video", media=("a.mp4",)))
    )


def test_with_a_public_address_configured_media_posts_are_allowed(tmp_path: Path) -> None:
    creds = credentials(tmp_path)
    env = {
        "META_CREDENTIALS_FILE": str(creds.path),
        "PUBLIC_MEDIA_DIR": str(tmp_path / "public"),
        "PUBLIC_MEDIA_BASE_URL": BASE,
    }
    publisher = build_live_publisher("threads", settings(), env, FakeThreads().transport())
    assert publisher.validate(snap("video", media=("a.mp4",))) == []


@pytest.mark.parametrize("env_key", ["META_CREDENTIALS_FILE"])
def test_a_live_threads_without_its_file_stops_start_up(env_key: str) -> None:
    with pytest.raises(ConfigError, match="META_CREDENTIALS_FILE"):
        build_live_publisher("threads", settings(), {})


def test_a_file_without_a_threads_user_id_is_refused(tmp_path: Path) -> None:
    creds = credentials(tmp_path, user_id="")
    with pytest.raises(ConfigError, match=r"threads\.user_id is empty"):
        build_live_publisher("threads", settings(), {"META_CREDENTIALS_FILE": str(creds.path)})


# --- renewing the 60-day token ------------------------------------------------------------------

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)


def test_the_token_is_renewed_and_its_expiry_recorded(tmp_path: Path) -> None:
    fake, creds = FakeThreads(), credentials(tmp_path)
    result = refresh_threads_token(creds, transport=fake.transport(), now=NOW)
    assert result.ok and result.changed and "good for 60 days" in result.message
    section = json.loads(creds.path.read_text())["threads"]
    assert section["access_token"] == fake.refreshed_token and section["user_id"] == "TH1"
    assert section["expires_at"].startswith("2026-12-01") and section["refreshed_at"].startswith(
        "2026-10-02"
    )
    assert days_left(creds, NOW) == 60


def test_a_recent_renewal_is_not_repeated_unless_forced(tmp_path: Path) -> None:
    fake = FakeThreads()
    creds = credentials(tmp_path, refreshed_at=(NOW - timedelta(days=2)).isoformat())
    skipped = refresh_threads_token(creds, transport=fake.transport(), now=NOW)
    assert (
        skipped.ok
        and not skipped.changed
        and "nothing to do" in skipped.message
        and fake.requests == []
    )
    forced = refresh_threads_token(creds, transport=fake.transport(), now=NOW, force=True)
    assert forced.changed


def test_an_old_renewal_is_repeated(tmp_path: Path) -> None:
    creds = credentials(tmp_path, refreshed_at=(NOW - timedelta(days=9)).isoformat())
    assert refresh_threads_token(creds, transport=FakeThreads().transport(), now=NOW).changed


def test_a_token_threads_refuses_to_renew_is_reported_and_left_in_place(tmp_path: Path) -> None:
    creds = credentials(tmp_path, access_token="expired-for-real")
    result = refresh_threads_token(creds, transport=FakeThreads().transport(), now=NOW)
    assert not result.ok and "refused to refresh" in result.message
    assert json.loads(creds.path.read_text())["threads"]["access_token"] == "expired-for-real"


def test_an_empty_token_cannot_be_renewed(tmp_path: Path) -> None:
    assert not refresh_threads_token(credentials(tmp_path, access_token=""), now=NOW).ok


def test_the_generic_refresh_renews_what_can_be_renewed(tmp_path: Path) -> None:
    creds = credentials(tmp_path)
    result = refresh_expiring_tokens(
        creds, load_platforms(DEFAULT_PLATFORMS_CONFIG), transport=FakeThreads().transport()
    )
    assert result.ok and result.changed


# --- `dk meta check` for Threads ------------------------------------------------------------------


def test_the_check_confirms_the_account_and_how_long_the_token_lasts(tmp_path: Path) -> None:
    creds = credentials(tmp_path, expires_at=(NOW + timedelta(days=50)).isoformat())
    report = check_threads(creds, transport=FakeThreads().transport(), now=NOW)
    text = "\n".join(report.lines)
    assert (
        report.problems == []
        and "works for @dhakakacchi (id TH1)" in text
        and "50 days left" in text
    )


def test_a_token_close_to_expiry_fails_the_check_with_the_fix(tmp_path: Path) -> None:
    creds = credentials(tmp_path, expires_at=(NOW + timedelta(days=5)).isoformat())
    report = check_threads(creds, transport=FakeThreads().transport(), now=NOW)
    assert any("expires in 5 days" in p and "make meta-refresh" in p for p in report.problems)


def test_an_unknown_expiry_is_a_todo_not_a_failure(tmp_path: Path) -> None:
    report = check_threads(credentials(tmp_path), transport=FakeThreads().transport(), now=NOW)
    assert report.problems == [] and any("expiry is unknown" in line for line in report.lines)


def test_a_token_for_someone_else_or_a_wrong_token_is_reported(tmp_path: Path) -> None:
    other = check_threads(
        credentials(tmp_path, user_id="SOMEONE"), transport=FakeThreads().transport(), now=NOW
    )
    assert any("belongs to Threads user TH1, not SOMEONE" in p for p in other.problems)
    wrong = check_threads(
        credentials(tmp_path, access_token="garbage"), transport=FakeThreads().transport(), now=NOW
    )
    assert any("Threads refused the token" in p for p in wrong.problems)


def test_empty_token_or_user_id_are_named(tmp_path: Path) -> None:
    assert any(
        "empty" in line for line in check_threads(credentials(tmp_path, access_token="")).lines
    )
    assert check_threads(credentials(tmp_path, user_id="")).problems == [
        "threads.user_id is empty in the credentials file"
    ]
