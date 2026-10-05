"""`dk live-test` against the stateful fakes: it must refuse to post without --yes and must prove
the post exists by reading it back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.support.fake_graph import TOKEN, FakeGraph
from tests.support.fake_threads import THREADS_TOKEN, FakeThreads

from dk_publishing.adapters.config.platforms import ConfigError, load_platforms
from dk_publishing.adapters.platforms.live_test import run_live_test
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG

PLATFORMS = load_platforms(DEFAULT_PLATFORMS_CONFIG)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "dk.json"
    path.write_text(
        json.dumps(
            {
                "meta": {
                    "facebook": {"page_id": "PAGE", "access_token": TOKEN},
                    "threads": {"user_id": "TH1", "access_token": THREADS_TOKEN},
                    "instagram": {"account_id": "IG1", "access_token": "z"},
                }
            }
        )
    )
    return {"META_CREDENTIALS_FILE": f"{path}#meta"}


def test_without_yes_nothing_is_posted(env: dict[str, str]) -> None:
    graph = FakeGraph()
    result = run_live_test("facebook", env, PLATFORMS["facebook"], transport=graph.transport())
    assert not result.ok and "Nothing was posted" in result.lines[-1]
    assert graph.requests == []


def test_a_facebook_text_post_is_published_and_read_back(env: dict[str, str]) -> None:
    graph = FakeGraph()
    result = run_live_test(
        "facebook",
        env,
        PLATFORMS["facebook"],
        text="hello",
        confirmed=True,
        transport=graph.transport(),
    )
    assert result.ok, result.lines
    text = "\n".join(result.lines)
    assert "published: id" in text and "read back from the platform" in text and "Delete" in text


def test_a_threads_text_post_is_published_and_read_back(env: dict[str, str]) -> None:
    threads = FakeThreads()
    result = run_live_test(
        "threads", env, PLATFORMS["threads"], confirmed=True, transport=threads.transport()
    )
    assert result.ok, result.lines
    assert len(threads.posted()) == 1 and "DK Publishing test post" in threads.posted()[0]["text"]


def test_a_failing_platform_is_reported_not_raised(env: dict[str, str]) -> None:
    import httpx

    graph = FakeGraph()
    graph.fail_next(httpx.Response(400, json={"error": {"message": "bad", "code": 100}}))
    result = run_live_test(
        "facebook", env, PLATFORMS["facebook"], confirmed=True, transport=graph.transport()
    )
    assert not result.ok and any("[FAIL]" in line for line in result.lines)


def test_platforms_that_need_a_video_say_so_and_unknown_ones_are_refused(
    env: dict[str, str], tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="needs a video file"):
        run_live_test("instagram", env, PLATFORMS["instagram"])
    with pytest.raises(ConfigError, match="no such video file"):
        run_live_test("youtube", env, PLATFORMS["youtube"], video=tmp_path / "none.mp4")
    with pytest.raises(ConfigError, match="no live test for 'linkedin'"):
        run_live_test("linkedin", env, PLATFORMS["linkedin"])


def test_youtube_is_always_tested_as_a_private_video(env: dict[str, str], tmp_path: Path) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    result = run_live_test("youtube", env, PLATFORMS["youtube"], video=clip)
    assert "PRIVATE video" in result.lines[0]
