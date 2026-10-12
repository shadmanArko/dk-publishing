"""GET /v1/spec: the publishing contract, from the same rules the engine enforces."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dk_publishing.adapters.platforms import facebook
from dk_publishing.application.use_cases.api_posts import STATE
from dk_publishing.application.use_cases.publishing_spec import STATES
from tests.integration.test_api import SLOT, Rig


@pytest.fixture
def rig(conninfo: str, tmp_path: Path) -> Rig:
    return Rig(conninfo, tmp_path)


def test_the_contract_states_every_platform_format_and_rule(rig: Rig) -> None:
    response = rig.call("GET", "/v1/spec", "read")
    assert response.status_code == 200
    spec = response.json()
    assert spec["spec"] == "publishing-spec/v1" and len(spec["spec_version"]) == 16
    assert response.headers["etag"] == f'"{spec["spec_version"]}"'
    assert set(spec["platforms"]) == set(rig.api.guides)
    assert set(spec["states"]) == set(STATE.values())  # every state a client can see is explained
    assert spec["post"]["variants"]["max"] == 20 and spec["post"]["slot"]["min_lead_s"] == 60
    for name, platform in spec["platforms"].items():
        assert platform["mode"] == "live" and platform["accounts"] == ["Dhaka Kacchi"]
        assert platform["timing"]["max_lateness_s"] > 0 and platform["route"]["how"]
        for fmt in platform["formats"].values():
            example = {**fmt["example"], "platform": name}
            assert Draft202012Validator(fmt["json_schema"]).is_valid(example)
    photo = spec["platforms"]["facebook"]["formats"]["photo"]["media"]
    assert photo["max_bytes"] == facebook.MAX_PHOTO_BYTES and ".jpg" in photo["types"]


def test_the_contract_is_cached_by_its_version_and_can_be_narrowed(rig: Rig) -> None:
    first = rig.call("GET", "/v1/spec", "read")
    auth = {"Authorization": f"Bearer {rig.keys['read']}", "If-None-Match": first.headers["etag"]}
    again = rig.client.get("/v1/spec", headers=auth)
    assert again.status_code == 304 and again.content == b""
    reel = rig.call("GET", "/v1/spec?platform=instagram&format=reel", "read").json()
    assert list(reel["platforms"]) == ["instagram"]
    assert list(reel["platforms"]["instagram"]["formats"]) == ["reel"]
    assert reel["spec_version"] == first.json()["spec_version"]  # a slice of the same contract
    assert rig.call("GET", "/v1/spec?platform=nowhere", "read").status_code == 404
    assert rig.call("GET", "/v1/spec?platform=instagram&format=nope", "read").status_code == 404
    assert rig.call("GET", "/v1/spec?format=reel", "read").status_code == 422
    assert rig.client.get("/v1/spec").status_code == 401


def test_a_new_account_changes_the_version(rig: Rig) -> None:
    before = rig.call("GET", "/v1/spec", "read").json()["spec_version"]
    with rig.api.core.uow() as uow:
        uow.sync.add_account("dk", "facebook", "Second Page", "page-2")
        uow.commit()
    after = rig.call("GET", "/v1/spec", "read").json()
    assert after["spec_version"] != before
    assert after["platforms"]["facebook"]["accounts"] == ["Dhaka Kacchi", "Second Page"]


def test_a_file_over_the_format_limit_is_refused_when_submitted(rig: Rig) -> None:
    big = rig.upload("platter.jpg", b"x" * (facebook.MAX_PHOTO_BYTES + 1))
    body = {
        "post_key": "too-big",
        "variants": [{"platform": "facebook", "format": "photo", "media": [big], "slot": SLOT}],
    }
    response = rig.call("POST", "/v1/posts/validate", json=body).json()
    assert response["valid"] is False
    (problem,) = response["problems"]
    assert problem["code"] == "file_too_large" and problem["path"] == "variants[0].media[0]"
    assert problem["limit"] == facebook.MAX_PHOTO_BYTES and problem["actual"] > problem["limit"]


def test_every_state_has_a_meaning_and_an_action() -> None:
    assert set(STATES) == set(STATE.values())
    assert all(s["means"] and s["do"] for s in STATES.values())
