"""POST /v1/posts/batch: many posts in one call, each on its own, always drafts, safe to repeat."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from dk_publishing.application.use_cases.api_batch import MAX_BATCH, submit_batch
from dk_publishing.entrypoints.publish_api.app import BatchIn, _request
from tests.integration.test_api import SLOT, Rig


@pytest.fixture
def rig(conninfo: str, tmp_path: Path) -> Rig:
    return Rig(conninfo, tmp_path)


def text_post(key: str, caption: str = "Open today from noon.") -> dict[str, Any]:
    variant = {"platform": "facebook", "format": "post", "caption": caption, "slot": SLOT}
    return {"post_key": key, "variants": [variant]}


def week(rig: Rig) -> list[dict[str, Any]]:
    return [rig.reel("b-reel"), text_post("b-text"), text_post("b-bad", caption="")]


def batch(rig: Rig, posts: list[dict[str, Any]], scope: str = "draft", **more: Any) -> Any:
    return rig.call("POST", "/v1/posts/batch", scope, json={"posts": posts, **more})


def keys_on_calendar(rig: Rig) -> list[str]:
    return sorted(p["post_key"] for p in rig.call("GET", "/v1/calendar", "read").json()["posts"])


def test_a_dry_run_checks_every_post_and_creates_nothing(rig: Rig) -> None:
    answer = batch(rig, week(rig), dry_run=True)
    assert answer.status_code == 200
    body = answer.json()
    assert body["dry_run"] is True and (body["ok"], body["failed"]) == (2, 1)
    bad = body["results"][2]
    assert bad["post_key"] == "b-bad" and bad["problems"][0]["code"] == "required"
    assert bad["examples"]  # a valid example comes with every problem
    assert len(body["spec_version"]) == 16
    assert keys_on_calendar(rig) == []


def test_good_posts_are_created_as_drafts_and_a_bad_one_does_not_stop_them(rig: Rig) -> None:
    posts = week(rig)
    first = batch(rig, posts).json()
    assert (first["ok"], first["failed"]) == (2, 1)
    assert [r["post"]["summary"] for r in first["results"][:2]] == ["awaiting_approval"] * 2
    assert first["results"][2]["error"] == "invalid"
    again = batch(rig, posts).json()  # the whole batch sent again: nothing doubles
    assert [r.get("replayed") for r in again["results"][:2]] == [True, True]
    assert keys_on_calendar(rig) == ["b-reel", "b-text"]
    assert len(rig.notifier.sent) == 2  # one note per new draft, none for the repeat


def test_a_batch_only_ever_makes_drafts_even_with_an_approve_key(rig: Rig) -> None:
    body = batch(rig, [{**text_post("b-approve"), "approve": True}], "approve").json()
    assert body["results"][0]["post"]["summary"] == "awaiting_approval"


def test_a_read_key_cannot_submit_and_the_size_is_capped(rig: Rig) -> None:
    assert batch(rig, [text_post("x")], "read").status_code == 403
    assert batch(rig, []).status_code == 422
    too_many = [text_post(f"k{i}") for i in range(MAX_BATCH + 1)]
    assert batch(rig, too_many).status_code == 422


def test_a_linked_file_is_downloaded_once_and_used_like_an_upload(
    conninfo: str, tmp_path: Path
) -> None:
    calls: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"remote-video")

    rig = Rig(
        conninfo, tmp_path, transport=httpx.MockTransport(serve),
        resolver=lambda h: [h if h[0].isdigit() else "93.184.216.34"],
    )  # fmt: skip
    link = "https://cdn.example.com/a/clip.mp4"
    posts = []
    for key in ("l-1", "l-2"):
        post = rig.reel(key)
        post["variants"][0]["media"] = [link]
        posts.append(post)
    body = batch(rig, posts).json()
    assert body["ok"] == 2 and calls == [link]
    refused = rig.reel("l-3")
    refused["variants"][0]["media"] = ["https://10.0.0.5/a.mp4"]  # a private address
    result = batch(rig, [refused]).json()["results"][0]
    assert result["ok"] is False and result["problems"][0]["path"] == "variants[0].media[0]"


def test_two_batches_racing_with_the_same_post_create_it_once(rig: Rig) -> None:
    requests = [_request(BatchIn.model_validate({"posts": [text_post("race")]}).posts[0])]

    def send(_: int) -> bool:
        items = submit_batch(rig.api, requests, actor_name="draft-key", dry_run=False)
        return items[0].ok

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(send, range(4)))
    assert keys_on_calendar(rig) == ["race"]
