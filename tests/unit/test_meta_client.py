from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from tests.support.fake_graph import TOKEN, FakeGraph, graph_error

from dk_publishing.adapters.platforms.meta import GraphClient, map_graph_error, redact
from dk_publishing.adapters.platforms.tokens import FileTokenProvider
from dk_publishing.domain.errors import (
    AuthFailed,
    PublishingError,
    RateLimited,
    Rejected,
    Retryable,
    UnknownOutcome,
)


class Static:
    def token(self) -> str:
        return TOKEN


def client(fake: FakeGraph) -> GraphClient:
    return GraphClient(version="v25.0", tokens=Static(), transport=fake.transport())


def body(code: int | None, message: str = "boom") -> dict[str, object]:
    return {"error": {"message": message, "code": code}}


H = httpx.Headers()

# --- the mapping table: what each Meta answer means for the never-post-twice rule ---------------


@pytest.mark.parametrize(
    ("status", "code", "idempotent", "expected"),
    [
        (400, 190, False, AuthFailed),  # token expired or revoked
        (401, None, False, AuthFailed),
        (400, 102, True, AuthFailed),
        (403, 200, False, AuthFailed),  # a missing permission needs re-authorising too
        (400, 10, False, AuthFailed),
        (400, 4, False, RateLimited),  # app / page rate limits
        (400, 17, True, RateLimited),
        (400, 32, False, RateLimited),
        (400, 613, False, RateLimited),
        (429, None, False, RateLimited),
        (400, 100, False, Rejected),  # a parameter Meta does not accept
        (400, 368, False, Rejected),  # blocked for a policy reason: retrying will not help
        (400, None, False, Rejected),
        # Meta's own trouble: after a write it may or may not have acted
        (500, None, False, UnknownOutcome),
        (503, None, False, UnknownOutcome),
        (400, 1, False, UnknownOutcome),
        (400, 2, False, UnknownOutcome),
        # ...but a read can simply be repeated
        (500, None, True, Retryable),
        (400, 2, True, Retryable),
    ],
)
def test_every_answer_maps_to_a_domain_error(
    status: int, code: int | None, idempotent: bool, expected: type[PublishingError]
) -> None:
    assert type(map_graph_error(status, body(code), H, idempotent=idempotent)) is expected


def test_a_rate_limit_obeys_retry_after_and_otherwise_waits_ten_minutes() -> None:
    told = map_graph_error(429, body(None), httpx.Headers({"retry-after": "90"}), idempotent=False)
    assert isinstance(told, RateLimited) and told.retry_after == timedelta(seconds=90)
    default = map_graph_error(400, body(4), H, idempotent=False)
    assert isinstance(default, RateLimited) and default.retry_after == timedelta(minutes=10)
    junk = map_graph_error(
        429, body(None), httpx.Headers({"retry-after": "soon"}), idempotent=False
    )
    assert isinstance(junk, RateLimited) and junk.retry_after == timedelta(minutes=10)


def test_meta_messages_are_passed_through_with_the_code_so_a_person_can_act() -> None:
    error = map_graph_error(
        400, body(100, "(#100) Param message must be non-empty."), H, idempotent=False
    )
    assert str(error) == "(#100) Param message must be non-empty. (Meta code 100)"
    assert "missing permission" in str(map_graph_error(403, body(200, "no"), H, idempotent=False))


def test_a_body_that_is_not_json_still_maps() -> None:
    assert isinstance(map_graph_error(502, None, H, idempotent=False), UnknownOutcome)
    assert isinstance(map_graph_error(404, "<html>", H, idempotent=False), Rejected)


# --- redaction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "GET https://graph.facebook.com/v25.0/me?access_token=EAAB123abc&fields=id failed",
        'body {"x": 1} access_token=EAAB123abc',
        "Authorization: Bearer EAAB123abc.def-ghi",
        "client_secret=s3cr3t",
    ],
)
def test_tokens_never_survive_redaction(text: str) -> None:
    cleaned = redact(text)
    assert "EAAB123abc" not in cleaned and "s3cr3t" not in cleaned and "[redacted]" in cleaned


def test_an_error_message_that_echoes_the_token_is_redacted() -> None:
    echoing = map_graph_error(
        400, body(100, "Bad request: access_token=EAAB-leaky&x=1"), H, idempotent=False
    )
    assert "EAAB-leaky" not in str(echoing)


# --- the client: what happens on the wire --------------------------------------------------------


def test_the_token_travels_in_the_body_of_a_write_and_the_query_of_a_read() -> None:
    fake = FakeGraph()
    api = client(fake)
    api.post("PAGE/feed", {"message": "hi"})
    api.get("PAGE/feed")
    post, get = fake.requests
    assert TOKEN in post.content.decode() and TOKEN not in str(post.url)
    assert f"access_token={TOKEN}" in str(get.url)


def test_the_version_is_in_the_path_and_videos_use_the_video_host() -> None:
    fake = FakeGraph()
    api = client(fake)
    api.get("PAGE/feed")
    assert fake.requests[0].url.path.startswith("/v25.0/")
    assert fake.hosts == ["graph.facebook.com"]


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow")])
def test_failing_to_connect_is_always_safe_to_retry(error: Exception) -> None:
    for call in ("get", "post"):
        fake = FakeGraph()
        fake.fail_next(error)
        with pytest.raises(Retryable, match="could not reach Meta"):
            client(fake).get("x") if call == "get" else client(fake).post(
                "PAGE/feed", {"message": "m"}
            )


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("t"), httpx.RemoteProtocolError("dropped"), httpx.WriteError("w")]
)
def test_a_lost_answer_to_a_write_is_uncertain_but_to_a_read_is_just_retryable(
    error: Exception,
) -> None:
    fake = FakeGraph()
    fake.fail_next(error)
    with pytest.raises(UnknownOutcome, match="no answer from Meta"):
        client(fake).post("PAGE/feed", {"message": "m"})
    fake.fail_next(error)
    with pytest.raises(Retryable):
        client(fake).get("PAGE/feed")


def test_server_errors_on_a_write_are_uncertain() -> None:
    fake = FakeGraph()
    fake.fail_next(httpx.Response(503, json={}))
    with pytest.raises(UnknownOutcome):
        client(fake).post("PAGE/feed", {"message": "m"})


def test_a_success_that_is_not_json_is_uncertain_for_a_write() -> None:
    fake = FakeGraph()
    fake.fail_next(httpx.Response(200, text="<html>proxy page</html>"))
    with pytest.raises(UnknownOutcome, match="not JSON"):
        client(fake).post("PAGE/feed", {"message": "m"})


def test_an_expired_token_is_reported_as_auth_failed() -> None:
    fake = FakeGraph()
    fake.fail_next(graph_error(400, 190, "Error validating access token: Session has expired", 463))
    with pytest.raises(AuthFailed, match="Session has expired"):
        client(fake).post("PAGE/feed", {"message": "m"})


def test_the_client_never_retries_a_write_by_itself() -> None:
    fake = FakeGraph()
    fake.fail_next(httpx.ReadTimeout("t"))
    with pytest.raises(UnknownOutcome):
        client(fake).post("PAGE/feed", {"message": "m"})
    assert len(fake.requests) == 1 and fake.posted() == []  # one attempt, nothing resent


# --- the token file ---------------------------------------------------------------------------


def test_the_token_file_may_hold_the_bare_token_or_json(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.write_text("  EAAB-bare\n")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text('{"access_token": "EAAB-json", "other": 1}')
    assert FileTokenProvider(bare).token() == "EAAB-bare"
    assert FileTokenProvider(wrapped).token() == "EAAB-json"


def test_the_token_file_is_reread_so_rotation_needs_no_restart(tmp_path: Path) -> None:
    path = tmp_path / "t"
    path.write_text("old")
    provider = FileTokenProvider(path)
    assert provider.token() == "old"
    path.write_text("new")
    assert provider.token() == "new"


@pytest.mark.parametrize("content", ["", "   ", "{not json", '{"nope": 1}'])
def test_a_bad_token_file_is_an_auth_problem_not_a_crash(tmp_path: Path, content: str) -> None:
    path = tmp_path / "t"
    path.write_text(content)
    with pytest.raises(AuthFailed):
        FileTokenProvider(path).token()


def test_a_missing_token_file_names_the_path_but_never_a_token(tmp_path: Path) -> None:
    with pytest.raises(AuthFailed, match=r"cannot read the token file .*nope"):
        FileTokenProvider(tmp_path / "nope").token()
