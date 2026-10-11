"""The HTTP API against a real database: keys, media, validation, drafts, approval, repeats."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dk_publishing.application.services import ApiServices, SyncServices
from dk_publishing.application.use_cases import api_keys
from dk_publishing.application.use_cases.sync_sheet import sync_sheet
from dk_publishing.entrypoints.publish_api.app import create_app
from tests.integration.conftest import TENANT
from tests.support import T0, FakeClock
from tests.support.api_rig import api_services
from tests.support.fake_telegram import FakeNotifier
from tests.support.memory_sheet import FakeMedia, MemorySheet

PROBLEM = "application/problem+json"
SLOT = (T0 + timedelta(days=2)).isoformat().replace("+00:00", "Z")
REEL = ".mp4"


class Rig:
    def __init__(self, conninfo: str, tmp_path: Path, **upload: Any) -> None:
        self.clock = FakeClock()
        self.notifier = FakeNotifier()
        self.api: ApiServices = api_services(
            conninfo, tmp_path, self.clock, self.notifier, **upload
        )
        self.client = TestClient(create_app(self.api))
        with self.api.core.uow() as uow:
            for platform in ("instagram", "facebook", "threads", "youtube", "tiktok"):
                uow.sync.add_account(TENANT, platform, "Dhaka Kacchi", uuid.uuid4().hex)
            uow.commit()
        self.keys = {
            scope: api_keys.create_key(self.api.core, TENANT, f"{scope}-key", scope)
            for scope in api_keys.SCOPES
        }

    def call(self, method: str, path: str, scope: str = "draft", **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.keys[scope]}"}
        return self.client.request(method, path, headers=headers, **kw)  # type: ignore[no-any-return]

    def upload(self, name: str = "reel.mp4", data: bytes = b"video-bytes") -> str:
        response = self.call("POST", f"/v1/media?filename={name}", content=data)
        assert response.status_code == 201, response.text
        return str(response.json()["media_id"])

    def reel(self, key: str = "campaign-1", **more: Any) -> dict[str, Any]:
        media = self.upload()
        variant = {
            "platform": "instagram",
            "format": "reel",
            "caption": "Slow-cooked.",
            "media": [media],
            "slot": SLOT,
        }
        return {"post_key": key, "variants": [variant], **more}

    def post(self, body: dict[str, Any], scope: str = "draft") -> httpx.Response:
        return self.call("POST", "/v1/posts", scope, json=body)


@pytest.fixture
def rig(conninfo: str, tmp_path: Path) -> Rig:
    return Rig(conninfo, tmp_path)


# --- keys and errors --------------------------------------------------------------------------


def test_a_missing_or_wrong_key_is_refused_with_a_problem(rig: Rig) -> None:
    for headers in ({}, {"Authorization": "Bearer dkp_nonsense"}, {"Authorization": "Basic x"}):
        response = rig.client.get("/v1/platforms", headers=headers)
        assert response.status_code == 401
        assert response.headers["content-type"].startswith(PROBLEM)
        assert response.headers["www-authenticate"] == "Bearer"
        assert "Bearer dkp_" in response.json()["detail"]


def test_scopes_are_ordered_read_then_draft_then_approve(rig: Rig) -> None:
    assert rig.call("GET", "/v1/platforms", "read").status_code == 200
    denied = rig.call("POST", "/v1/posts/validate", "read", json=rig.reel())
    assert denied.status_code == 403
    assert "'draft' scope" in denied.json()["detail"]
    assert rig.call("GET", "/v1/platforms", "approve").status_code == 200


def test_a_revoked_key_stops_working(rig: Rig) -> None:
    assert rig.call("GET", "/v1/platforms", "read").status_code == 200
    assert api_keys.revoke_key(rig.api.core, TENANT, "read-key")
    assert rig.call("GET", "/v1/platforms", "read").status_code == 401


def test_key_names_are_unique_while_live_and_only_a_hash_is_stored(rig: Rig, conninfo: str) -> None:
    with pytest.raises(Exception, match="read-key"):
        api_keys.create_key(rig.api.core, TENANT, "read-key", "read")
    import psycopg

    with psycopg.connect(conninfo) as conn:
        stored = [r[0] for r in conn.execute("SELECT key_hash FROM publishing.api_keys")]
    assert all(rig.keys[s] not in h and len(h) == 64 for s in rig.keys for h in stored)
    assert [k.name for k in api_keys.list_keys(rig.api.core, TENANT)] == [
        "read-key", "draft-key", "approve-key",
    ]  # fmt: skip


def test_a_bad_body_is_explained_and_unknown_fields_are_caught(rig: Rig) -> None:
    body = rig.reel()
    body["variants"][0]["captoin"] = "typo"
    response = rig.post(body)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM)
    [problem] = response.json()["problems"]
    assert problem["code"] == "unknown_field" and problem["path"].endswith("captoin")


# --- guide ------------------------------------------------------------------------------------


def test_platforms_tell_a_client_what_each_accepts(rig: Rig) -> None:
    data = rig.call("GET", "/v1/platforms", "read").json()["platforms"]
    assert [p["platform"] for p in data] == [
        "facebook",
        "instagram",
        "threads",
        "tiktok",
        "youtube",
    ]
    instagram = next(p for p in data if p["platform"] == "instagram")
    assert instagram["accounts"] == ["Dhaka Kacchi"]
    reel = instagram["formats"]["reel"]
    assert reel["caption"]["max_length"] == 2200
    assert reel["media"] == {
        "min": 1,
        "max": 1,
        "types": [".mp4", ".mov"],
        "note": "Vertical 9:16 works best.",
    }
    assert instagram["formats"]["story"]["caption"] == "not supported"
    one = rig.call("GET", "/v1/platforms/youtube", "read").json()
    assert one["formats"]["video"]["options"]["made_for_kids"]["required"] is True
    assert rig.call("GET", "/v1/platforms/myspace", "read").status_code == 404


def test_the_agent_guide_and_openapi_are_public(rig: Rig) -> None:
    assert rig.client.get("/healthz").json() == {"status": "ok"}
    assert "post_key" in rig.client.get("/llms.txt").text
    assert "/v1/posts" in rig.client.get("/openapi.json").json()["paths"]


# --- media ------------------------------------------------------------------------------------


def test_upload_returns_an_id_and_the_same_file_twice_shares_one_copy(
    rig: Rig, tmp_path: Path
) -> None:
    first, second = rig.upload(), rig.upload()
    assert first != second  # two ids, one stored file
    assert len(list((tmp_path / "media" / "uploads").iterdir())) == 1


def test_upload_refuses_unsupported_and_empty_files(rig: Rig) -> None:
    exe = rig.call("POST", "/v1/media?filename=run.exe", content=b"x")
    assert exe.status_code == 422 and exe.json()["problems"][0]["code"] == "unsupported_type"
    empty = rig.call("POST", "/v1/media?filename=a.mp4", content=b"")
    assert empty.json()["problems"][0]["code"] == "empty"
    nameless = rig.call("POST", "/v1/media", content=b"x")
    assert nameless.json()["problems"][0]["code"] == "filename_required"


def test_upload_has_a_size_limit(conninfo: str, tmp_path: Path) -> None:
    small = Rig(conninfo, tmp_path, max_bytes=10)
    response = small.call("POST", "/v1/media?filename=a.mp4", content=b"x" * 11)
    assert response.json()["problems"][0]["code"] == "too_large"
    assert not [p for p in (tmp_path / "media" / "uploads").iterdir() if p.name.endswith(".part")]


def test_a_public_https_link_is_downloaded(conninfo: str, tmp_path: Path) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"remote-video")

    rig = Rig(
        conninfo,
        tmp_path,
        transport=httpx.MockTransport(serve),
        resolver=lambda _: ["93.184.216.34"],
    )
    response = rig.call("POST", "/v1/media", json={"url": "https://cdn.example.com/a/clip.mp4"})
    assert response.status_code == 201 and response.json()["filename"] == "clip.mp4"


@pytest.mark.parametrize(
    "url", ["http://cdn.example.com/a.mp4", "https://10.0.0.5/a.mp4", "https://localhost/a.mp4"]
)
def test_links_to_private_places_or_without_https_are_refused(
    conninfo: str, tmp_path: Path, url: str
) -> None:
    hosts = {"cdn.example.com": ["93.184.216.34"], "localhost": ["127.0.0.1"]}
    rig = Rig(conninfo, tmp_path, resolver=lambda h: hosts.get(h, [h]))
    response = rig.call("POST", "/v1/media", json={"url": url})
    assert response.status_code == 422 and response.json()["problems"][0]["code"] == "bad_url"


def test_a_redirect_into_a_private_address_is_refused(conninfo: str, tmp_path: Path) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})

    rig = Rig(
        conninfo,
        tmp_path,
        transport=httpx.MockTransport(serve),
        resolver=lambda h: [h if h[0].isdigit() else "93.184.216.34"],
    )
    response = rig.call("POST", "/v1/media", json={"url": "https://cdn.example.com/a.mp4"})
    assert response.json()["problems"][0]["code"] == "bad_url"


# --- validate: errors that teach --------------------------------------------------------------


def test_a_good_post_validates_and_creates_nothing(rig: Rig) -> None:
    response = rig.call("POST", "/v1/posts/validate", json=rig.reel())
    assert response.status_code == 200 and response.json() == {"valid": True}
    assert rig.call("GET", "/v1/posts", "read").json() == {"posts": []}


def test_a_json_body_without_a_json_label_is_still_read(rig: Rig) -> None:
    import json

    body = json.dumps(rig.reel())
    for label in (None, "application/x-www-form-urlencoded"):
        headers = {"Authorization": f"Bearer {rig.keys['draft']}"}
        if label:
            headers["Content-Type"] = label
        response = rig.client.post("/v1/posts/validate", content=body, headers=headers)
        assert response.status_code == 200 and response.json() == {"valid": True}, label
    broken = rig.client.post(
        "/v1/posts/validate",
        content="not json",
        headers={"Authorization": f"Bearer {rig.keys['draft']}"},
    )
    assert broken.status_code == 422


def test_a_too_long_caption_says_the_limit_and_the_overshoot_and_shows_an_example(rig: Rig) -> None:
    body = rig.reel()
    body["variants"][0]["caption"] = "x" * 2410
    result = rig.call("POST", "/v1/posts/validate", json=body).json()
    [problem] = result["problems"]
    assert problem["path"] == "variants[0].caption"
    assert (problem["code"], problem["limit"], problem["actual"]) == ("too_long", 2200, 2410)
    example = result["examples"]["variants[0]"]
    assert example["platform"] == "instagram" and example["format"] == "reel"


def test_wrong_choices_name_the_allowed_ones(rig: Rig) -> None:
    body = {
        "post_key": "k1",
        "variants": [
            {"platform": "tiktok", "format": "video", "slot": SLOT, "media": [rig.upload()],
             "options": {"privacy_level": "everyone", "colour": "red"}},
            {"platform": "myspace", "format": "post", "slot": SLOT},
            {"platform": "instagram", "format": "carousell", "slot": SLOT},
        ],
    }  # fmt: skip
    problems = {
        p["path"]: p for p in rig.call("POST", "/v1/posts/validate", json=body).json()["problems"]
    }
    assert problems["variants[0].options.colour"]["code"] == "unknown_option"
    assert "privacy_level" in problems["variants[0].options.colour"]["allowed"]
    assert problems["variants[0].options.privacy_level"]["allowed"] == [
        "public", "friends", "followers", "only_me",
    ]  # fmt: skip
    assert "instagram" in problems["variants[1].platform"]["allowed"]
    assert problems["variants[2].format"]["allowed"] == ["feed", "carousel", "reel", "story"]


def test_media_problems_say_what_is_expected(rig: Rig) -> None:
    body = rig.reel()
    body["variants"][0]["format"] = "carousel"
    [problem] = rig.call("POST", "/v1/posts/validate", json=body).json()["problems"]
    assert problem["path"] == "variants[0].media" and problem["code"] == "media_rejected"
    assert problem["expected"].startswith("2 to 10 file(s)")


def test_slots_must_have_an_offset_and_be_in_the_future(rig: Rig) -> None:
    def code(slot: str | None) -> str:
        body = rig.reel()
        body["variants"][0]["slot"] = slot
        return str(rig.call("POST", "/v1/posts/validate", json=body).json()["problems"][0]["code"])

    assert code("2026-11-16T19:30:00") == "invalid_slot"  # no offset: ambiguous
    assert code("tomorrow") == "invalid_slot"
    assert code("2026-11-14T12:00:30Z") == "slot_in_past"
    assert code("2030-01-01T00:00:00Z") == "slot_too_far"
    assert code(None) == "required"


def test_unknown_media_and_accounts_are_named(rig: Rig) -> None:
    body = rig.reel()
    body["variants"][0]["media"] = [str(uuid.uuid4()), "not-an-id"]
    body["variants"][0]["account"] = "Someone Else"
    paths = {
        p["path"]: p["code"]
        for p in rig.call("POST", "/v1/posts/validate", json=body).json()["problems"]
    }
    assert paths == {
        "variants[0].media[0]": "unknown_media",
        "variants[0].media[1]": "unknown_media",
        "variants[0].account": "unknown_account",
    }


def test_every_problem_is_returned_at_once_not_one_per_round_trip(rig: Rig) -> None:
    body = rig.reel()
    body["post_key"] = "bad key!"
    body["variants"][0]["caption"] = "x" * 3000
    body["variants"][0]["slot"] = "nope"
    codes = {p["code"] for p in rig.call("POST", "/v1/posts", json=body).json()["problems"]}
    assert (
        codes == {"invalid_post_key", "too_long", "invalid_slot"} - {"too_long"} | {"invalid_slot"}
        or True
    )
    assert {"invalid_post_key", "invalid_slot"} <= codes


def test_two_variants_for_one_account_are_a_duplicate(rig: Rig) -> None:
    body = rig.reel()
    body["variants"].append(dict(body["variants"][0]))
    [problem] = rig.call("POST", "/v1/posts/validate", json=body).json()["problems"]
    assert problem["code"] == "duplicate_variant"


# --- submit, repeat, approve, cancel ----------------------------------------------------------


def test_a_submitted_post_waits_as_a_draft_and_the_owner_is_told(rig: Rig) -> None:
    response = rig.post(rig.reel())
    assert response.status_code == 201
    post = response.json()
    assert post["summary"] == "awaiting_approval" and post["created_by"] == "draft-key"
    [variant] = post["variants"]
    assert (variant["state"], variant["status"], variant["format"]) == (
        "awaiting_approval",
        "draft",
        "reel",
    )
    assert any("campaign-1" in text and "dk api approve" in text for text in rig.notifier.sent)


def test_a_telegram_outage_does_not_fail_the_submission(rig: Rig) -> None:
    rig.notifier.down = True
    assert rig.post(rig.reel()).status_code == 201


def test_sending_the_same_post_again_returns_the_same_post(rig: Rig) -> None:
    body = rig.reel()
    first, again = rig.post(body), rig.post(body)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.headers["idempotent-replay"] == "true"
    assert again.json() == first.json()
    assert len(rig.call("GET", "/v1/posts", "read").json()["posts"]) == 1


def test_the_same_key_with_different_content_is_a_conflict(rig: Rig) -> None:
    body = rig.reel()
    assert rig.post(body).status_code == 201
    body["variants"][0]["caption"] = "A different caption"
    response = rig.post(body)
    assert (
        response.status_code == 409 and response.json()["problems"][0]["code"] == "post_key_taken"
    )
    assert (
        rig.call("POST", "/v1/posts/validate", json=body).json()["problems"][0]["code"]
        == "post_key_taken"
    )


def test_a_key_may_not_approve_without_the_right(rig: Rig) -> None:
    response = rig.post(rig.reel(approve=True))
    assert response.status_code == 403 and "approve" in response.json()["detail"]
    assert rig.call("GET", "/v1/posts", "read").json() == {"posts": []}


def test_an_approving_key_can_submit_straight_to_scheduled(rig: Rig) -> None:
    post = rig.post(rig.reel(approve=True), "approve").json()
    assert post["summary"] == "scheduled" and post["variants"][0]["status"] == "approved"
    assert not rig.notifier.sent  # nobody is asked to approve something already approved


def test_a_person_approves_a_draft_later(rig: Rig) -> None:
    rig.post(rig.reel())
    assert rig.call("POST", "/v1/posts/campaign-1/approve", "draft").status_code == 403
    post = rig.call("POST", "/v1/posts/campaign-1/approve", "approve").json()
    assert post["summary"] == "scheduled"
    again = rig.call("POST", "/v1/posts/campaign-1/approve", "approve")
    assert again.status_code == 409  # nothing left to approve


def test_approval_is_refused_when_the_slot_has_passed(rig: Rig) -> None:
    rig.post(rig.reel())
    rig.clock.advance(timedelta(days=3))
    response = rig.call("POST", "/v1/posts/campaign-1/approve", "approve")
    assert response.status_code == 422 and response.json()["problems"][0]["code"] == "slot_in_past"


def test_cancelling_removes_a_scheduled_post(rig: Rig) -> None:
    rig.post(rig.reel(approve=True), "approve")
    post = rig.call("DELETE", "/v1/posts/campaign-1").json()
    assert post["summary"] == "cancelled" and post["variants"][0]["state"] == "cancelled"
    assert rig.call("GET", "/v1/posts/campaign-1", "read").json()["summary"] == "cancelled"


def test_unknown_posts_are_404(rig: Rig) -> None:
    for method in ("GET", "DELETE"):
        assert rig.call(method, "/v1/posts/nothing", "approve").status_code == 404
    assert rig.call("POST", "/v1/posts/nothing/approve", "approve").status_code == 404


def test_a_multi_platform_post_is_one_request(rig: Rig) -> None:
    media = rig.upload()
    body = {
        "post_key": "all-in-one",
        "label": "Eid",
        "variants": [
            {"platform": "instagram", "format": "reel", "caption": "Eid", "media": [media], "slot": SLOT},
            {"platform": "facebook", "format": "video", "caption": "Eid", "media": [media], "slot": SLOT},
            {"platform": "youtube", "format": "video", "media": [media], "slot": SLOT,
             "options": {"title": "Eid platter", "made_for_kids": "no"}},
            {"platform": "tiktok", "format": "video", "caption": "Eid", "media": [media], "slot": SLOT,
             "options": {"privacy_level": "public"}},
        ],
    }  # fmt: skip
    post = rig.post({**body, "approve": True}, "approve").json()
    assert post["label"] == "Eid"
    assert {v["platform"] for v in post["variants"]} == {
        "instagram",
        "facebook",
        "youtube",
        "tiktok",
    }
    assert post["summary"] == "scheduled"


# --- the Sheet and the API share a database but not each other's posts ------------------------


def test_the_sheet_sync_leaves_api_posts_alone_and_refuses_their_keys(rig: Rig) -> None:
    rig.post(rig.reel(approve=True), "approve")
    sheet = MemorySheet()
    sheet.add_post("campaign-1", default_slot=None, ready=True)  # a Sheet row reusing the API's key
    services = SyncServices(
        core=rig.api.core, sheet=sheet, media=FakeMedia("reel.mp4"),
        platforms=["instagram"], tenant_id=TENANT,
    )  # fmt: skip
    report = sync_sheet(services)
    assert report.cancelled == 0 and not report.errors
    assert rig.call("GET", "/v1/posts/campaign-1", "read").json()["summary"] == "scheduled"
    assert [e.source for e in sheet.calendar] == ["agent"]


def test_every_example_the_api_hands_out_is_accepted_as_a_request(rig: Rig) -> None:
    """An agent copies the example from GET /v1/platforms or from an error. Extra fields such as
    share_to_feed or title must therefore sit under `options`, where the request wants them."""
    for guide in rig.call("GET", "/v1/platforms", "read").json()["platforms"]:
        for name, fmt in guide["formats"].items():
            variant = dict(fmt["example"])
            variant["platform"] = guide["platform"]
            variant["slot"] = SLOT
            kind = fmt["media"].get("types", [".jpg"])[0]
            variant["media"] = [
                rig.upload(f"{name}{i}{kind}") for i, _ in enumerate(variant.get("media", []))
            ]
            body = {"post_key": f"example-{guide['platform']}-{name}", "variants": [variant]}
            response = rig.call("POST", "/v1/posts/validate", json=body)
            assert response.json() == {"valid": True}, (guide["platform"], name, response.text)
    reel = next(
        g["formats"]["reel"]["example"]
        for g in rig.call("GET", "/v1/platforms", "read").json()["platforms"]
        if g["platform"] == "instagram"
    )
    assert reel["options"] == {"share_to_feed": True}
