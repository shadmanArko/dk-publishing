"""TikTok Direct Post: the publisher's rules, the HTTP client and the sign-in."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from tests.support import CAPS, T0
from tests.support.fake_tiktok import FakeTikTok, MemoryLog

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.platforms.connect import connect_tiktok
from dk_publishing.adapters.platforms.tiktok_api import (
    HttpTikTokApi,
    TikTokCredentials,
    TikTokTokens,
    UploadSlot,
    authorize_url,
    exchange_code,
    map_error,
)
from dk_publishing.adapters.platforms.tiktok_direct import (
    CHUNK,
    SMALL,
    TikTokDirectPublisher,
    chunking,
)
from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)
from dk_publishing.domain.publishing import Rendition, VariantSnapshot

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def snap(**content: Any) -> VariantSnapshot:
    merged = {
        "caption": "Kacchi tonight",
        "privacy_level": "only_me",
        "allow_comments": True,
        "commercial_disclosure": True,
        "media": [{"name": "clip.mp4"}],
        **content,
    }
    return VariantSnapshot("v1", "dk", "tiktok", "acct", T0, merged)


def clip(tmp_path: Path, data: bytes = b"VIDEO") -> list[Rendition]:
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    return [Rendition("original", str(path), "sha")]


def build(api: FakeTikTok | None = None, log: MemoryLog | None = None, **kw: Any) -> Any:
    return TikTokDirectPublisher(
        api=api or FakeTikTok(),
        capabilities=CAPS,
        log=log or MemoryLog(),
        sleep=lambda _: None,
        **kw,
    )


# --- chunking follows TikTok's rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (1, (1, 1)),
        (4_000_000, (4_000_000, 1)),  # under 5 MB: one piece, chunk size = file size
        (SMALL, (SMALL, 1)),
        (SMALL + 1, (CHUNK, 1)),  # the last chunk absorbs the remainder
        (3 * CHUNK + 5, (CHUNK, 3)),
        (50 * CHUNK, (CHUNK, 50)),
    ],
)
def test_chunk_count_is_the_size_divided_by_the_chunk_rounded_down(
    size: int, expected: tuple[int, int]
) -> None:
    assert chunking(size) == expected


# --- preparing and posting ---------------------------------------------------------------------------


def test_prepare_checks_the_privacy_choice_against_what_tiktok_offers_for_the_account(
    tmp_path: Path,
) -> None:
    api = FakeTikTok(options=("SELF_ONLY",))
    publisher = build(api)
    handle = publisher.prepare(snap(), clip(tmp_path))
    assert handle["post_info"]["privacy_level"] == "SELF_ONLY"
    assert handle["username"] == "dhakakacchi" and api.inits == []  # nothing started yet

    with pytest.raises(Rejected, match="does not allow 'public' for this account"):
        publisher.prepare(snap(privacy_level="public"), clip(tmp_path))


def test_the_rows_choices_become_tiktoks_post_settings(tmp_path: Path) -> None:
    api = FakeTikTok(options=("SELF_ONLY", "PUBLIC_TO_EVERYONE"))
    handle = build(api).prepare(
        snap(privacy_level="public", allow_comments=False, allow_duet=True), clip(tmp_path)
    )
    assert handle["post_info"] == {
        "title": "Kacchi tonight",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "disable_comment": True,
        "disable_duet": False,
        "disable_stitch": True,
        "brand_organic_toggle": True,
        "brand_content_toggle": False,
    }


def test_publishing_starts_records_uploads_and_waits(tmp_path: Path) -> None:
    api, log = FakeTikTok(), MemoryLog()
    api.polls_before_done = 2
    publisher = build(api, log)
    live = publisher.publish(publisher.prepare(snap(), clip(tmp_path, b"REAL-BYTES")))
    assert live.external_id.startswith("v_pub_file") and live.url is None  # private: no address
    assert api.uploads == [b"REAL-BYTES"]
    assert api.inits[0]["size"] == 10 and api.inits[0]["chunks"] == 1
    # the record is written before the upload happens
    assert api.calls.index("init_video") < api.calls.index("upload")
    assert log.keys == [f"tiktok:dk:v1:{live.external_id}"]


def test_a_crash_between_starting_and_uploading_is_settled_without_a_second_post(
    tmp_path: Path,
) -> None:
    api, log = FakeTikTok(), MemoryLog()
    publisher = build(api, log)
    handle = publisher.prepare(snap(), clip(tmp_path))
    api.fail_next = None
    original_upload = api.upload

    def dies(*args: Any, **kwargs: Any) -> None:
        raise UnknownOutcome("the process died while uploading")

    api.upload = dies  # type: ignore[method-assign]
    with pytest.raises(UnknownOutcome):
        publisher.publish(handle)
    api.upload = original_upload  # type: ignore[method-assign]

    # The post was started but never uploaded, so TikTok has nothing to publish: "processing".
    with pytest.raises(Retryable, match="still processing"):
        publisher.find_live(snap(), handle)
    assert len(api.inits) == 1


def test_find_live_says_not_live_when_nothing_was_ever_started(tmp_path: Path) -> None:
    assert build().find_live(snap(), None) is None


def test_a_post_tiktok_rejected_is_not_live_and_may_be_retried(tmp_path: Path) -> None:
    api, log = FakeTikTok(), MemoryLog()
    api.final_status = "FAILED"
    publisher = build(api, log)
    with pytest.raises(Rejected, match="file_format_check_failed"):
        publisher.publish(publisher.prepare(snap(), clip(tmp_path)))
    assert publisher.find_live(snap(), None) is None  # failed on TikTok's side: confirmed not live


def test_tiktok_taking_too_long_is_uncertain_not_failed(tmp_path: Path) -> None:
    api = FakeTikTok()
    api.polls_before_done = 10_000
    publisher = build(api, wait=10.0, poll_every=5.0)
    with pytest.raises(UnknownOutcome, match="still processing"):
        publisher.publish(publisher.prepare(snap(), clip(tmp_path)))


def test_the_public_address_appears_once_tiktok_has_published_it(tmp_path: Path) -> None:
    from dk_publishing.adapters.platforms.tiktok_api import PostStatus

    done = PostStatus("PUBLISH_COMPLETE", None, ("7000123",))
    live = TikTokDirectPublisher._live("pid", done, "dhakakacchi")
    assert live.url == "https://www.tiktok.com/@dhakakacchi/video/7000123"


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(Rejected, match="not downloaded"):
        build().prepare(snap(), [])
    with pytest.raises(Rejected, match="is missing"):
        build().prepare(snap(), [Rendition("original", str(tmp_path / "x.mp4"), "s")])
    with pytest.raises(Rejected, match="missing what it needs"):
        build().publish({"kind": "direct"})


# --- the HTTP client ----------------------------------------------------------------------------------


class Server:
    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "error": {"code": "ok", "message": ""}})


def err(code: str, status: int = 200, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"data": {}, "error": {"code": code, "message": message}})


class FixedTokens:
    def token(self) -> str:
        return "act.secret-token"


def api(server: Server) -> HttpTikTokApi:
    return HttpTikTokApi(FixedTokens(), server.transport)  # type: ignore[arg-type]


def test_creator_info_is_read_with_the_bearer_token() -> None:
    server = Server(
        ok(
            {
                "creator_username": "dhakakacchi",
                "creator_nickname": "Dhaka Kacchi",
                "privacy_level_options": ["SELF_ONLY"],
                "comment_disabled": False,
                "max_video_post_duration_sec": 600,
            }
        )
    )
    info = api(server).creator_info()
    assert info.username == "dhakakacchi" and info.privacy_options == ("SELF_ONLY",)
    [request] = server.requests
    assert request.url.path == "/v2/post/publish/creator_info/query/"
    assert request.headers["authorization"] == "Bearer act.secret-token"


def test_a_post_is_started_with_the_file_size_and_chunks() -> None:
    server = Server(ok({"publish_id": "P1", "upload_url": "https://up.example/x"}))
    slot = api(server).init_video(post_info={"title": "t"}, size=20, chunk=20, chunks=1)
    assert slot == UploadSlot("P1", "https://up.example/x")
    body = json.loads(server.requests[0].content)
    assert body["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": 20,
        "chunk_size": 20,
        "total_chunk_count": 1,
    }


def test_the_file_goes_up_in_order_with_content_range_headers(tmp_path: Path) -> None:
    path = tmp_path / "big.mp4"
    path.write_bytes(b"a" * 25)
    server = Server(httpx.Response(206), httpx.Response(201))
    api(server).upload(UploadSlot("P", "https://up.example/x"), str(path), chunk=10)
    first, second = server.requests
    assert first.headers["content-range"] == "bytes 0-9/25" and len(first.content) == 10
    assert (
        second.headers["content-range"] == "bytes 10-24/25" and len(second.content) == 15
    )  # takes the rest
    assert second.headers["content-type"] == "video/mp4"


def test_upload_problems_are_explained(tmp_path: Path) -> None:
    path = tmp_path / "c.mp4"
    path.write_bytes(b"x")
    slot = UploadSlot("P", "https://up.example/x")
    with pytest.raises(Retryable, match="expired"):
        api(Server(httpx.Response(403))).upload(slot, str(path), chunk=1)
    with pytest.raises(Rejected, match="HTTP 416"):
        api(Server(httpx.Response(416))).upload(slot, str(path), chunk=1)


def test_a_lost_answer_on_the_last_chunk_is_uncertain_but_earlier_ones_are_not(
    tmp_path: Path,
) -> None:
    path = tmp_path / "c.mp4"
    path.write_bytes(b"x" * 20)
    slot = UploadSlot("P", "https://up.example/x")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = HttpTikTokApi(FixedTokens(), httpx.MockTransport(broken))  # type: ignore[arg-type]
    with pytest.raises(Retryable):
        client.upload(slot, str(path), chunk=10)  # fails on the first of two chunks
    with pytest.raises(UnknownOutcome):
        client.upload(slot, str(path), chunk=20)  # the only (last) chunk


@pytest.mark.parametrize(
    ("code", "status", "write", "expected"),
    [
        ("access_token_invalid", 401, False, AuthFailed),
        ("scope_not_authorized", 401, True, AuthFailed),
        ("rate_limit_exceeded", 429, False, RateLimited),
        ("spam_risk_too_many_posts", 200, True, RateLimited),
        ("unaudited_client_can_only_post_to_private_accounts", 403, True, Rejected),
        ("privacy_level_option_mismatch", 403, True, Rejected),
        ("internal_error", 500, True, UnknownOutcome),  # a write that may have worked
        ("internal_error", 500, False, Retryable),
        ("something_new", 400, True, Rejected),
    ],
)
def test_every_tiktok_answer_is_one_of_the_five_outcomes(
    code: str, status: int, write: bool, expected: type[PublishingError]
) -> None:
    assert type(map_error(code, "msg", status, write=write)) is expected


def test_the_audit_restriction_is_explained_in_plain_words() -> None:
    error = map_error("unaudited_client_can_only_post_to_private_accounts", "", 403, write=True)
    assert "not passed its audit" in str(error) and "private account" in str(error)


def test_network_failures_follow_the_safe_rules() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("https://open.tiktokapis.com/secret-in-url")

    client = HttpTikTokApi(FixedTokens(), httpx.MockTransport(down))  # type: ignore[arg-type]
    with pytest.raises(Retryable) as caught:
        client.creator_info()
    assert "secret" not in str(caught.value)

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    lost_client = HttpTikTokApi(FixedTokens(), httpx.MockTransport(lost))  # type: ignore[arg-type]
    with pytest.raises(UnknownOutcome):
        lost_client.init_video(post_info={}, size=1, chunk=1, chunks=1)
    with pytest.raises(Retryable):
        lost_client.status("P")


def test_a_status_answer_is_read() -> None:
    server = Server(ok({"status": "FAILED", "fail_reason": "duration_check_failed"}))
    status = api(server).status("P")
    assert status.status == "FAILED" and status.fail_reason == "duration_check_failed"
    assert json.loads(server.requests[0].content) == {"publish_id": "P"}


# --- tokens and the sign-in -----------------------------------------------------------------------------


def creds(tmp_path: Path, **values: Any) -> TikTokCredentials:
    path = tmp_path / "dk.json"
    path.write_text(
        json.dumps(
            {
                "tiktok": {
                    "client_key": "KEY",
                    "client_secret": "SECRET",
                    "redirect_uri": "https://m.example/tiktok/callback",
                    **values,
                }
            }
        )
    )
    return TikTokCredentials(f"{path}#tiktok")


def token_reply(**more: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "act.new",
            "refresh_token": "rft.new",
            "expires_in": 86400,
            "refresh_expires_in": 31536000,
            "open_id": "OPEN",
            **more,
        },
    )


def test_the_authorize_address_carries_everything_tiktok_needs() -> None:
    url = authorize_url("KEY", "https://m.example/cb", "STATE")
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert query == {
        "client_key": ["KEY"],
        "scope": ["user.info.basic,video.publish"],
        "response_type": ["code"],
        "redirect_uri": ["https://m.example/cb"],
        "state": ["STATE"],
    }


def test_exchanging_the_code_stores_the_tokens_and_their_expiry(tmp_path: Path) -> None:
    server = Server(token_reply())
    c = creds(tmp_path)
    stored = exchange_code(c, "CODE", "https://m.example/cb", transport=server.transport, now=NOW)
    assert stored["access_token"] == "act.new" and c.load()["refresh_token"] == "rft.new"
    form = parse_qs(server.requests[0].content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["CODE"]
    assert form["client_secret"] == ["SECRET"]
    assert datetime.fromisoformat(c.load()["access_expires_at"]) == NOW + timedelta(days=1)


def test_a_valid_token_is_reused_and_an_old_one_is_renewed_and_saved(tmp_path: Path) -> None:
    fresh = creds(
        tmp_path,
        access_token="act.old",
        access_expires_at=(NOW + timedelta(hours=5)).isoformat(),
        refresh_token="rft.old",
    )
    server = Server()
    assert TikTokTokens(fresh, server.transport, lambda: NOW).token() == "act.old"
    assert server.requests == []

    stale = creds(
        tmp_path,
        access_token="act.old",
        access_expires_at=(NOW + timedelta(minutes=3)).isoformat(),
        refresh_token="rft.old",
        refresh_expires_at=(NOW + timedelta(days=100)).isoformat(),
    )
    server = Server(token_reply())
    assert TikTokTokens(stale, server.transport, lambda: NOW).token() == "act.new"
    assert parse_qs(server.requests[0].content.decode())["grant_type"] == ["refresh_token"]
    assert stale.load()["refresh_token"] == "rft.new"


def test_sign_in_problems_say_to_sign_in_again(tmp_path: Path) -> None:
    with pytest.raises(AuthFailed, match="no TikTok login yet"):
        TikTokTokens(creds(tmp_path), None, lambda: NOW).token()
    expired = creds(
        tmp_path, refresh_token="r", refresh_expires_at=(NOW - timedelta(days=1)).isoformat()
    )
    with pytest.raises(AuthFailed, match="expired"):
        TikTokTokens(expired, None, lambda: NOW).token()
    refused = creds(tmp_path, refresh_token="r")
    bad = Server(httpx.Response(400, json={"error": "invalid_grant", "error_description": "gone"}))
    with pytest.raises(AuthFailed, match="dk connect tiktok"):
        TikTokTokens(refused, bad.transport, lambda: NOW).token()
    with pytest.raises(ConfigError, match="client_key"):
        creds(tmp_path, client_key="", client_secret="").app()


def test_connect_signs_in_checks_the_state_and_names_the_account(tmp_path: Path) -> None:
    c = creds(tmp_path)
    info = ok(
        {
            "creator_username": "dhakakacchi",
            "creator_nickname": "Dhaka Kacchi",
            "privacy_level_options": ["SELF_ONLY"],
        }
    )
    server = Server(token_reply(), info)
    opened: list[str] = []
    result = connect_tiktok(
        c.path.as_posix() + "#tiktok",
        ask=lambda _: "https://m.example/tiktok/callback?code=CODE&state=S1&scopes=a",
        open_browser=lambda url: opened.append(url),
        transport=server.transport,
        state="S1",
    )
    assert result.ok, result.lines
    assert "@dhakakacchi" in result.lines[-1] and "SELF_ONLY" in result.lines[-1]
    assert opened and "state=S1" in opened[0]
    assert c.load()["access_token"] == "act.new"


@pytest.mark.parametrize(
    ("pasted", "words"),
    [
        ("https://m.example/cb?code=C&state=WRONG", "does not belong to this sign-in"),
        ("https://m.example/cb?error=access_denied&state=S1", "access_denied"),
        ("https://m.example/cb?state=S1", "no code"),
    ],
)
def test_connect_refuses_a_bad_address(tmp_path: Path, pasted: str, words: str) -> None:
    result = connect_tiktok(
        creds(tmp_path).path.as_posix() + "#tiktok",
        ask=lambda _: pasted,
        open_browser=lambda url: None,
        state="S1",
    )
    assert not result.ok and words in "\n".join(result.lines)


def test_connect_needs_the_app_details_and_a_redirect_address(tmp_path: Path) -> None:
    path = tmp_path / "dk.json"
    path.write_text(json.dumps({"tiktok": {"client_key": "", "client_secret": ""}}))
    assert "client_key" in connect_tiktok(f"{path}#tiktok").lines[0]
    path.write_text(json.dumps({"tiktok": {"client_key": "K", "client_secret": "S"}}))
    assert "redirect_uri" in connect_tiktok(f"{path}#tiktok").lines[0]


def test_connect_check_without_a_login_says_what_to_do(tmp_path: Path) -> None:
    result = connect_tiktok(creds(tmp_path).path.as_posix() + "#tiktok", check_only=True)
    assert not result.ok and "make connect-tiktok" in result.lines[-1]


# --- going live -------------------------------------------------------------------------------------


def test_tiktok_goes_live_from_dk_json_and_needs_the_publish_record(tmp_path: Path) -> None:
    from dk_publishing.adapters.config.platforms import load_platforms
    from dk_publishing.adapters.platforms.live import build_live_publisher
    from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG

    settings = load_platforms(DEFAULT_PLATFORMS_CONFIG)["tiktok"]
    ref = creds(tmp_path).path.as_posix() + "#tiktok"
    env = {"TIKTOK_CREDENTIALS_FILE": ref}
    assert isinstance(
        build_live_publisher("tiktok", settings, env, None, MemoryLog()), TikTokDirectPublisher
    )
    with pytest.raises(ConfigError, match="no tiktok section"):
        build_live_publisher("tiktok", settings, {}, None, MemoryLog())
    with pytest.raises(ConfigError, match="remember which posts"):
        build_live_publisher("tiktok", settings, env)


def test_the_live_test_posts_tiktok_privately_and_needs_a_video(tmp_path: Path) -> None:
    from dk_publishing.adapters.config.platforms import load_platforms
    from dk_publishing.adapters.platforms.live_test import run_live_test
    from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG

    settings = load_platforms(DEFAULT_PLATFORMS_CONFIG)["tiktok"]
    with pytest.raises(ConfigError, match="needs a video file"):
        run_live_test("tiktok", {}, settings)
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x")
    result = run_live_test("tiktok", {}, settings, video=video)
    assert "PRIVATE video" in result.lines[0] and "Nothing was posted" in result.lines[-1]


def test_signing_in_again_shows_the_sign_in_even_when_a_login_is_saved(tmp_path: Path) -> None:
    c = creds(tmp_path, access_token="act.old", refresh_token="rft.old")
    opened: list[str] = []
    server = Server(token_reply(), ok({"creator_username": "dk", "privacy_level_options": []}))
    result = connect_tiktok(
        c.path.as_posix() + "#tiktok",
        again=True,
        ask=lambda _: "https://m.example/cb?code=C&state=S1",
        open_browser=lambda url: opened.append(url),
        transport=server.transport,
        state="S1",
    )
    assert result.ok and opened and c.load()["access_token"] == "act.new"
    saved = connect_tiktok(
        c.path.as_posix() + "#tiktok",
        open_browser=lambda url: opened.append("x"),
        transport=Server(ok({"creator_username": "dk", "privacy_level_options": []})).transport,
    )
    assert saved.ok and "x" not in opened  # without --again a saved login is just checked


def test_signing_in_again_asks_tiktok_to_show_the_permissions_page_every_time() -> None:
    plain = parse_qs(urlparse(authorize_url("K", "https://m.example/cb", "S")).query)
    again = parse_qs(
        urlparse(authorize_url("K", "https://m.example/cb", "S", always_ask=True)).query
    )
    assert "disable_auto_auth" not in plain and again["disable_auto_auth"] == ["1"]
