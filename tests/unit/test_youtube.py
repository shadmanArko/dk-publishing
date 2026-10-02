from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response  # type: ignore[import-untyped]
from tests.support import NATIVE_CAPS, T0
from tests.support.fake_youtube import FakeYouTube

from dk_publishing.adapters.config.platforms import ConfigError, load_platforms
from dk_publishing.adapters.platforms.connect import connect_platform
from dk_publishing.adapters.platforms.live import build_live_publisher
from dk_publishing.adapters.platforms.youtube import YouTubePublisher
from dk_publishing.adapters.platforms.youtube_api import (
    GoogleYouTubeApi,
    YouTubeCredentials,
    map_google_error,
    write_template,
)
from dk_publishing.application.ports import NativeScheduler
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG
from dk_publishing.domain.errors import AuthFailed, RateLimited, Rejected, Retryable, UnknownOutcome
from dk_publishing.domain.publishing import Rendition, VariantSnapshot

SLOT = T0 + timedelta(hours=5)


def build(api: FakeYouTube | None = None) -> tuple[YouTubePublisher, FakeYouTube]:
    api = api or FakeYouTube(uploaded_at=SLOT)
    return YouTubePublisher(api=api, capabilities=NATIVE_CAPS, sleep=lambda _: None), api


def snap(**more: Any) -> VariantSnapshot:
    content = {
        "title": "Eid platter reel",
        "description": "Slow-cooked kacchi.",
        "tags": "kacchi, eid, berlin",
        "category": "Entertainment",
        "visibility_after_publish": "public",
        "made_for_kids": "no",
        "media": [{"name": "eid.mp4", "drive_file_id": "F", "md5": "m"}],
        **more,
    }
    return VariantSnapshot("v1", "dk", "youtube", "acct", SLOT, content)


def video(tmp_path: Path, data: bytes = b"VIDEOBYTES") -> list[Rendition]:
    path = tmp_path / "eid.mp4"
    path.write_bytes(data)
    return [Rendition("original", str(path), "sha")]


# --- validate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "field", "words"),
    [
        ({"title": ""}, "title", "needs a title"),
        ({"title": "x" * 101}, "title", "101 characters; YouTube allows 100"),
        ({"title": "a <b> c"}, "title", "cannot contain < or >"),
        ({"description": "é" * 3000}, "description", "longer than 5,000 bytes"),
        ({"tags": ", ".join(["tag"] * 200)}, "tags", "over 500 characters"),
        ({"category": "Gaming"}, "category", "must be one of: People & Blogs"),
        ({"visibility_after_publish": "secret"}, "visibility_after_publish", "must be one of"),
        ({"made_for_kids": None}, "made_for_kids", "explicit yes or no"),
        ({"made_for_kids": "maybe"}, "made_for_kids", "explicit yes or no"),
        ({"media": []}, "media", "exactly one video file"),
        ({"media": [{"name": "a.jpg"}]}, "media", "not a video file"),
        (
            {"delivery": "native", "visibility_after_publish": "private"},
            "visibility_after_publish",
            "pointless",
        ),
    ],
)
def test_bad_videos_are_explained(override: dict[str, Any], field: str, words: str) -> None:
    problems = build()[0].validate(snap(**override))
    assert any(p.field == field and words in p.message for p in problems), problems


def test_a_good_video_passes_and_the_caption_can_stand_in_for_the_description() -> None:
    publisher = build()[0]
    assert publisher.validate(snap()) == []
    assert publisher.validate(snap(description="", caption="From the caption column")) == []
    assert publisher.validate(snap(category=None, tags=None, visibility_after_publish=None)) == []


# --- direct publishing ----------------------------------------------------------------------------


def test_prepare_uploads_nothing_and_publish_uploads_the_file_with_the_right_metadata(
    tmp_path: Path,
) -> None:
    publisher, api = build()
    handle = publisher.prepare(snap(), video(tmp_path))
    assert api.uploads == [] and handle["file"].endswith("eid.mp4")

    live = publisher.publish(handle)
    assert live.external_id == "vid1" and live.url == "https://www.youtube.com/watch?v=vid1"
    [upload] = api.uploads
    assert upload["bytes"] == b"VIDEOBYTES"
    assert upload["body"] == {
        "snippet": {
            "title": "Eid platter reel",
            "description": "Slow-cooked kacchi.",
            "categoryId": "24",
            "tags": ["kacchi", "eid", "berlin"],
        },
        "status": {"selfDeclaredMadeForKids": False, "privacyStatus": "public"},
    }


def test_made_for_kids_yes_and_a_blank_visibility_default_correctly(tmp_path: Path) -> None:
    publisher, api = build()
    publisher.publish(
        publisher.prepare(
            snap(made_for_kids="yes", visibility_after_publish=None, category=None, tags=None),
            video(tmp_path),
        )
    )
    body = api.uploads[0]["body"]
    assert body["status"] == {"selfDeclaredMadeForKids": True, "privacyStatus": "public"}
    assert body["snippet"]["categoryId"] == "22" and "tags" not in body["snippet"]


def test_a_video_that_was_not_downloaded_or_vanished_is_rejected(tmp_path: Path) -> None:
    publisher, _ = build()
    with pytest.raises(Rejected, match="was not downloaded"):
        publisher.prepare(snap(), [])
    with pytest.raises(Rejected, match=r"gone\.mp4 is missing"):
        publisher.prepare(snap(), [Rendition("original", str(tmp_path / "gone.mp4"), "sha")])


def test_an_incomplete_handle_is_rejected() -> None:
    with pytest.raises(Rejected, match="missing what it needs"):
        build()[0].publish({"kind": "video"})


def test_an_unaudited_project_still_counts_as_uploaded_even_though_youtube_keeps_it_private(
    tmp_path: Path,
) -> None:
    publisher, api = build(FakeYouTube(force_private=True))
    live = publisher.publish(publisher.prepare(snap(), video(tmp_path)))
    assert live.external_id == "vid1"
    assert api.videos["vid1"]["status"]["privacyStatus"] == "private"  # what YouTube enforced


# --- never posting twice ----------------------------------------------------------------------


def test_a_lost_answer_after_the_upload_is_uncertain_and_the_video_is_then_found_not_reuploaded(
    tmp_path: Path,
) -> None:
    publisher, api = build()
    handle = publisher.prepare(snap(), video(tmp_path))
    api.lose_response_next = True
    with pytest.raises(UnknownOutcome):
        publisher.publish(handle)
    assert len(api.uploads) == 1  # YouTube really has it

    found = publisher.find_live(snap(), handle)
    assert found is not None and found.external_id == "vid1"
    assert len(api.uploads) == 1  # and it was never uploaded a second time


def test_find_live_says_not_live_when_there_is_no_match_or_it_is_old(tmp_path: Path) -> None:
    publisher, _api = build()
    assert publisher.find_live(snap(), None) is None
    publisher.publish(publisher.prepare(snap(title="Another video"), video(tmp_path)))
    assert publisher.find_live(snap(), None) is None
    old, old_api = build(FakeYouTube(uploaded_at=SLOT - timedelta(days=3)))
    old.publish(old.prepare(snap(), video(tmp_path)))
    assert old.find_live(snap(), None) is None and old_api.calls  # same title, but long ago


def test_when_youtube_cannot_answer_find_live_raises() -> None:
    publisher, api = build()
    api.fail_next = Retryable("quota endpoint down")
    with pytest.raises(Retryable):
        publisher.find_live(snap(), None)


# --- native scheduling ----------------------------------------------------------------------------


def test_the_adapter_can_hold_videos() -> None:
    assert isinstance(build()[0], NativeScheduler)


def test_a_scheduled_video_is_uploaded_now_as_private_with_the_exact_publish_time(
    tmp_path: Path,
) -> None:
    publisher, api = build()
    handle = publisher.schedule(snap(delivery="native"), video(tmp_path), SLOT)
    body = api.uploads[0]["body"]
    assert body["status"]["privacyStatus"] == "private"  # until the slot
    assert body["status"]["publishAt"] == SLOT.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert handle["scheduled_id"] == "vid1" and handle["scheduled_for"].startswith(
        SLOT.strftime("%Y-%m-%dT%H:%M")
    )


def test_the_publish_time_is_utc_whatever_zone_it_started_in(tmp_path: Path) -> None:
    publisher, api = build()
    from datetime import timezone

    publisher.schedule(snap(), video(tmp_path), SLOT.astimezone(timezone(timedelta(hours=2))))
    assert api.uploads[0]["body"]["status"]["publishAt"] == SLOT.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_a_held_video_is_not_live_until_youtube_makes_it_public(tmp_path: Path) -> None:
    publisher, api = build()
    handle = publisher.schedule(snap(), video(tmp_path), SLOT)
    api.now = SLOT - timedelta(minutes=1)
    assert publisher.find_live(snap(), handle) is None

    api.now = SLOT + timedelta(minutes=1)  # YouTube published it by itself
    found = publisher.find_live(snap(), handle)
    assert found is not None and found.url == "https://www.youtube.com/watch?v=vid1"


def test_an_unaudited_project_never_publishes_a_scheduled_video_and_that_is_visible(
    tmp_path: Path,
) -> None:
    publisher, api = build(FakeYouTube(force_private=True))
    handle = publisher.schedule(snap(), video(tmp_path), SLOT)
    api.now = SLOT + timedelta(hours=1)
    assert publisher.find_live(snap(), handle) is None  # still private: reconcile will say so


def test_a_scheduled_video_deleted_on_youtube_is_reported_not_assumed(tmp_path: Path) -> None:
    publisher, api = build()
    handle = publisher.schedule(snap(), video(tmp_path), SLOT)
    api.videos.clear()
    with pytest.raises(Rejected, match="no longer exists"):
        publisher.find_live(snap(), handle)


def test_cancelling_deletes_the_held_video_and_nothing_is_scheduled_otherwise(
    tmp_path: Path,
) -> None:
    publisher, api = build()
    handle = publisher.schedule(snap(), video(tmp_path), SLOT)
    publisher.cancel(handle)
    assert api.deleted == ["vid1"] and api.videos == {}
    with pytest.raises(Rejected, match="nothing was scheduled"):
        publisher.cancel({"kind": "video"})


# --- the real client, with Google's own shapes -------------------------------------------------------


class Request:
    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.retries: list[int] = []

    def next_chunk(self, num_retries: int = 0) -> tuple[Any, Any]:
        self.retries.append(num_retries)
        item = self.chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return None, item

    def execute(self) -> Any:
        return self.chunks.pop(0)


class Resource:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        def method(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            return self

        return method

    def insert(self, **kwargs: Any) -> Request:
        self.calls.append(("insert", kwargs))
        request: Request = self.replies["insert"]
        return request

    def list(self, **kwargs: Any) -> Request:
        self.calls.append(("list", kwargs))
        return Request([self.replies["list"].pop(0)])

    def delete(self, **kwargs: Any) -> Request:
        self.calls.append(("delete", kwargs))
        return Request([self.replies.get("delete", {})])


def http_error(status: int, reason: str = "", message: str = "") -> HttpError:
    body = json.dumps({"error": {"message": message, "errors": [{"reason": reason}]}}).encode()
    return HttpError(Response({"status": status}), body)


def test_the_upload_loops_over_chunks_until_youtube_answers_and_notifies_nobody(
    tmp_path: Path,
) -> None:
    reply = {"id": "abc", "status": {"privacyStatus": "private"}}
    request = Request([None, None, reply])
    resource = Resource({"insert": request})
    api = GoogleYouTubeApi(lambda: resource, media=lambda path: f"MEDIA:{path}")
    result = api.upload(body={"snippet": {}, "status": {}}, file_path="/tmp/v.mp4")
    assert (result.video_id, result.privacy) == ("abc", "private") and request.retries == [3, 3, 3]
    insert = next(kw for name, kw in resource.calls if name == "insert")
    assert insert["part"] == "snippet,status" and insert["notifySubscribers"] is False
    assert insert["media_body"] == "MEDIA:/tmp/v.mp4"


@pytest.mark.parametrize(
    ("exc", "write", "expected"),
    [
        (http_error(403, "quotaExceeded"), True, RateLimited),
        (http_error(403, "rateLimitExceeded"), False, RateLimited),
        (http_error(401), True, AuthFailed),
        (http_error(403, "forbidden"), True, AuthFailed),
        (http_error(403, "insufficientPermissions"), False, AuthFailed),
        (http_error(400, "invalidTitle", "bad title"), True, Rejected),
        (http_error(404, "videoNotFound"), False, Rejected),
        (http_error(500), True, UnknownOutcome),  # a write: it may have worked
        (http_error(503), False, Retryable),  # a read can simply be repeated
        (TimeoutError("timed out"), True, UnknownOutcome),
        (TimeoutError("timed out"), False, Retryable),
        (ConnectionRefusedError("no"), True, Retryable),  # never left this machine
    ],
)
def test_every_google_failure_is_one_of_the_five_outcomes(
    exc: BaseException, write: bool, expected: type[Exception]
) -> None:
    assert type(map_google_error(exc, write=write)) is expected


def test_an_expired_login_tells_you_how_to_reconnect() -> None:
    from google.auth.exceptions import RefreshError

    error = map_google_error(RefreshError("invalid_grant"), write=True)  # type: ignore[no-untyped-call]
    assert isinstance(error, AuthFailed) and "dk connect youtube" in str(error)


def test_a_quota_wait_is_an_hour() -> None:
    error = map_google_error(http_error(403, "quotaExceeded"), write=True)
    assert isinstance(error, RateLimited) and error.retry_after == timedelta(hours=1)


def test_a_failing_upload_surfaces_as_a_domain_error_never_a_google_exception() -> None:
    resource = Resource({"insert": Request([http_error(500)])})
    api = GoogleYouTubeApi(lambda: resource, media=lambda path: None)
    with pytest.raises(UnknownOutcome):
        api.upload(body={}, file_path="x")


def test_recent_uploads_follow_the_uploads_playlist_and_keep_youtubes_order() -> None:
    def item(vid: str, title: str) -> dict[str, Any]:
        return {
            "id": vid,
            "snippet": {"title": title, "publishedAt": "2026-11-14T17:00:05Z"},
            "status": {"privacyStatus": "public"},
        }

    resource = Resource(
        {
            "list": [
                {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]},
                {
                    "items": [
                        {"snippet": {"resourceId": {"videoId": "b"}}},
                        {"snippet": {"resourceId": {"videoId": "a"}}},
                    ]
                },
                {"items": [item("a", "First"), item("b", "Second")]},
            ]
        }
    )
    videos = GoogleYouTubeApi(lambda: resource).recent_uploads()
    assert [v.video_id for v in videos] == ["b", "a"] and videos[0].title == "Second"


def test_a_video_by_id_and_a_missing_one() -> None:
    found = Resource(
        {
            "list": [
                {
                    "items": [
                        {
                            "id": "a",
                            "snippet": {"title": "T"},
                            "status": {
                                "privacyStatus": "private",
                                "publishAt": "2026-11-14T17:00:00.000Z",
                            },
                        }
                    ]
                }
            ]
        }
    )
    info = GoogleYouTubeApi(lambda: found).video("a")
    assert (
        info is not None
        and info.privacy == "private"
        and info.scheduled_for == "2026-11-14T17:00:00.000Z"
    )
    assert GoogleYouTubeApi(lambda: Resource({"list": [{"items": []}]})).video("zzz") is None


def test_deleting_a_video_that_is_already_gone_is_fine_but_other_errors_are_not() -> None:
    class Deleting(Resource):
        def delete(self, **kwargs: Any) -> Request:
            raise self.replies["error"]

    GoogleYouTubeApi(lambda: Deleting({"error": http_error(404, "videoNotFound")})).delete("x")
    with pytest.raises(AuthFailed):
        GoogleYouTubeApi(lambda: Deleting({"error": http_error(401)})).delete("x")


# --- the credentials file and the one-time connect --------------------------------------------------


def test_the_template_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "youtube.json"
    assert write_template(path) is True
    assert (
        stat.S_IMODE(path.stat().st_mode) == 0o600
        and "OUTSIDE the git repository" in path.read_text()
    )
    path.write_text('{"mine": 1}')
    assert write_template(path) is False and path.read_text() == '{"mine": 1}'


def test_a_login_without_a_refresh_token_says_to_connect(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    path.write_text(json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": ""}))
    with pytest.raises(AuthFailed, match="refresh_token; run `dk connect youtube`"):
        YouTubeCredentials(path).google_credentials()


def test_a_complete_file_builds_google_credentials_with_the_needed_scopes(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    path.write_text(json.dumps({"client_id": "a", "client_secret": "b", "refresh_token": "r"}))
    creds = YouTubeCredentials(path).google_credentials()
    assert (
        creds.refresh_token == "r"
        and "https://www.googleapis.com/auth/youtube.upload" in creds.scopes
    )


def test_a_broken_file_is_explained(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    path.write_text("{nope")
    with pytest.raises(ConfigError, match="not valid JSON"):
        YouTubeCredentials(path).load()
    with pytest.raises(ConfigError, match="cannot read"):
        YouTubeCredentials(tmp_path / "missing.json").load()
    path.write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        YouTubeCredentials(path).load()


class FakeChannel:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    def channel(self) -> tuple[str, str]:
        if self.fail:
            raise self.fail
        return "UC123", "Dhaka Kacchi"


def channel_factory(fail: Exception | None = None) -> Any:
    return lambda _credentials: FakeChannel(fail)


def test_connect_first_creates_the_template_then_asks_for_the_client(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    first = connect_platform("youtube", path)
    assert first.ok and "created" in first.lines[0] and path.exists()
    second = connect_platform("youtube", path)
    assert not second.ok and "fill in client_id and client_secret" in second.lines[0]


def test_connect_runs_the_consent_once_stores_the_token_and_names_the_channel(
    tmp_path: Path,
) -> None:
    path = tmp_path / "y.json"
    path.write_text(
        json.dumps(
            {"client_id": "id", "client_secret": "secret", "refresh_token": "", "channel_id": ""}
        )
    )
    asked: list[tuple[str, str]] = []

    def consent(client_id: str, secret: str) -> str:
        asked.append((client_id, secret))
        return "REFRESH-TOKEN"

    result = connect_platform("youtube", path, consent=consent, api_factory=channel_factory())
    assert result.ok and "connected to the YouTube channel 'Dhaka Kacchi' (id UC123)" in "\n".join(
        result.lines
    )
    data = json.loads(path.read_text())
    assert (
        data["refresh_token"] == "REFRESH-TOKEN"
        and data["channel_id"] == "UC123"
        and asked == [("id", "secret")]
    )

    again = connect_platform("youtube", path, consent=consent, api_factory=channel_factory())
    assert again.ok and len(asked) == 1  # the consent is not repeated


def test_connect_check_never_opens_a_browser(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    path.write_text(json.dumps({"client_id": "id", "client_secret": "s", "refresh_token": "r"}))

    def never(*_: str) -> str:
        raise AssertionError("must not run the consent in check mode")

    ok = connect_platform(
        "youtube", path, check_only=True, consent=never, api_factory=channel_factory()
    )
    assert ok.ok
    broken = connect_platform(
        "youtube",
        path,
        check_only=True,
        consent=never,
        api_factory=channel_factory(AuthFailed("revoked")),
    )
    assert not broken.ok and "refused the saved login" in broken.lines[-1]
    path.write_text(json.dumps({"client_id": "id", "client_secret": "s", "refresh_token": ""}))
    assert "no refresh_token yet" in connect_platform("youtube", path, check_only=True).lines[-1]


def test_a_consent_that_fails_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    path.write_text(json.dumps({"client_id": "id", "client_secret": "s", "refresh_token": ""}))

    def refuse(*_: str) -> str:
        raise AuthFailed("Google returned no refresh token")

    result = connect_platform("youtube", path, consent=refuse)
    assert not result.ok and "no refresh token" in result.lines[0]


def test_only_youtube_has_a_connect_step(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no connect step"):
        connect_platform("instagram", tmp_path / "x.json")


# --- going live ------------------------------------------------------------------------------------


def settings() -> Any:
    return load_platforms(DEFAULT_PLATFORMS_CONFIG)["youtube"]


def test_youtube_goes_live_from_its_credentials_file(tmp_path: Path) -> None:
    path = tmp_path / "y.json"
    path.write_text(json.dumps({"client_id": "id", "client_secret": "s", "refresh_token": ""}))
    publisher = build_live_publisher("youtube", settings(), {"YOUTUBE_CREDENTIALS_FILE": str(path)})
    assert isinstance(publisher, YouTubePublisher)  # no network at start-up; tokens are read later
    assert publisher.capabilities.native_window == (timedelta(minutes=15), timedelta(days=180))


def test_live_youtube_without_its_file_stops_start_up(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="YOUTUBE_CREDENTIALS_FILE"):
        build_live_publisher("youtube", settings(), {})
    with pytest.raises(ConfigError, match="cannot read the YouTube credentials file"):
        build_live_publisher(
            "youtube", settings(), {"YOUTUBE_CREDENTIALS_FILE": str(tmp_path / "nope.json")}
        )
    assert datetime.now(UTC)


# --- a fresh upload can be missing from YouTube's list for a while ---------------------------------


def lagging(lag: int, naps: list[float]) -> tuple[YouTubePublisher, FakeYouTube]:
    api = FakeYouTube(uploaded_at=SLOT)
    api.list_lag = lag
    return YouTubePublisher(api=api, capabilities=NATIVE_CAPS, sleep=naps.append), api


def test_a_fresh_upload_missing_from_the_list_is_found_on_a_later_check(tmp_path: Path) -> None:
    naps: list[float] = []
    publisher, api = lagging(0, naps)
    publisher.publish(publisher.prepare(snap(), video(tmp_path)))
    api.list_lag = 2  # the next two listings do not show it yet
    found = publisher.find_live(snap(), None)
    assert found is not None and found.external_id == "vid1"
    assert naps == [15.0, 15.0]  # it waited between the checks, and stopped at the first hit


def test_a_video_that_really_is_absent_is_only_declared_so_after_waiting(tmp_path: Path) -> None:
    naps: list[float] = []
    publisher, _ = lagging(0, naps)
    assert publisher.find_live(snap(), None) is None
    assert naps == [15.0, 15.0, 15.0]  # four checks, three waits: about 45 seconds


def test_a_video_that_shows_up_at_once_costs_no_waiting(tmp_path: Path) -> None:
    naps: list[float] = []
    publisher, _ = lagging(0, naps)
    publisher.publish(publisher.prepare(snap(), video(tmp_path)))
    assert publisher.find_live(snap(), None) is not None and naps == []
