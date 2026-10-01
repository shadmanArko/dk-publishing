from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest
from tests.support.fake_graph import TOKEN, FakeGraph

from dk_publishing.adapters.config.platforms import ConfigError, PlatformSettings, load_platforms
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.live import build_live_publisher
from dk_publishing.adapters.platforms.meta_check import check_facebook, check_meta
from dk_publishing.adapters.platforms.meta_credentials import (
    MetaCredentials,
    SectionTokenProvider,
    write_template_from_env,
)
from dk_publishing.composition import DEFAULT_PLATFORMS_CONFIG
from dk_publishing.domain.errors import AuthFailed

ENV = {
    "META_APP_ID": "936388502452682",
    "FACEBOOK_PAGE_ID": "PAGE",
    "INSTAGRAM_ACCOUNT_ID": "IG1",
    "THREADS_USER_ID": "TH1",
}


def make(tmp_path: Path, **overrides: object) -> MetaCredentials:
    path = tmp_path / "meta.json"
    data: dict[str, object] = {
        "app_id": "936388502452682",
        "app_secret": "APPSECRET",
        "facebook": {"page_id": "PAGE", "access_token": TOKEN},
        **overrides,
    }
    path.write_text(json.dumps(data))
    return MetaCredentials(path)


# --- the file ---------------------------------------------------------------------------------


def test_the_template_has_the_ids_but_no_tokens_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "meta.json"
    assert write_template_from_env(path, ENV) is True
    data = json.loads(path.read_text())
    assert data["app_id"] == "936388502452682"
    assert data["facebook"] == {"page_id": "PAGE", "access_token": ""}
    assert data["instagram"]["account_id"] == "IG1" and data["threads"]["user_id"] == "TH1"
    assert data["app_secret"] == "" and "OUTSIDE the git repository" in data["_help"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # nobody else on this computer can read it


def test_an_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    path.write_text('{"precious": true}')
    assert write_template_from_env(path, ENV) is False
    assert path.read_text() == '{"precious": true}'


def test_a_broken_file_is_explained(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    path.write_text('{"facebook": {')
    with pytest.raises(ConfigError, match="not valid JSON"):
        MetaCredentials(path).section("facebook")
    with pytest.raises(ConfigError, match="cannot read"):
        MetaCredentials(tmp_path / "nope.json").section("facebook")
    path.write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        MetaCredentials(path).section("facebook")
    path.write_text("{}")
    with pytest.raises(ConfigError, match="no 'facebook' section"):
        MetaCredentials(path).section("facebook")


def test_the_token_is_reread_so_pasting_a_new_one_needs_no_restart(tmp_path: Path) -> None:
    credentials = make(tmp_path, facebook={"page_id": "PAGE", "access_token": "old"})
    provider = SectionTokenProvider(credentials, "facebook")
    assert provider.token() == "old"
    make(tmp_path, facebook={"page_id": "PAGE", "access_token": "new"})
    assert provider.token() == "new"


def test_an_empty_token_is_an_auth_problem_that_says_where_to_paste(tmp_path: Path) -> None:
    credentials = make(tmp_path, facebook={"page_id": "PAGE", "access_token": " "})
    with pytest.raises(AuthFailed, match="no access_token for 'facebook'"):
        SectionTokenProvider(credentials, "facebook").token()


# --- the live adapter reads it ----------------------------------------------------------------


def settings() -> PlatformSettings:
    return load_platforms(DEFAULT_PLATFORMS_CONFIG)["facebook"]


def test_one_file_is_enough_to_build_the_live_adapter(tmp_path: Path) -> None:
    credentials = make(tmp_path)
    publisher = build_live_publisher(
        "facebook",
        settings(),
        {"META_CREDENTIALS_FILE": str(credentials.path)},
        FakeGraph().transport(),
    )
    assert isinstance(publisher, FacebookPublisher)


def test_the_page_id_in_the_file_is_what_gets_used(tmp_path: Path) -> None:
    credentials = make(tmp_path)
    fake = FakeGraph()
    publisher = build_live_publisher(
        "facebook", settings(), {"META_CREDENTIALS_FILE": str(credentials.path)}, fake.transport()
    )
    from tests.support import T0

    from dk_publishing.domain.publishing import VariantSnapshot

    snap = VariantSnapshot(
        "v", "dk", "facebook", "a", T0, {"format": "post", "caption": "hi", "media": []}
    )
    publisher.publish(publisher.prepare(snap, []))
    assert fake.requests[0].url.path == "/v25.0/PAGE/feed"


def test_a_file_without_a_page_id_is_refused_at_start_up(tmp_path: Path) -> None:
    credentials = make(tmp_path, facebook={"page_id": "", "access_token": "x"})
    with pytest.raises(ConfigError, match=r"facebook\.page_id is empty"):
        build_live_publisher(
            "facebook", settings(), {"META_CREDENTIALS_FILE": str(credentials.path)}
        )


# --- `dk meta check` --------------------------------------------------------------------------


def run(tmp_path: Path, fake: FakeGraph | None = None, **overrides: object):  # type: ignore[no-untyped-def]
    fake = fake or FakeGraph()
    return check_facebook(make(tmp_path, **overrides), version="v25.0", transport=fake.transport())


def test_an_empty_token_is_a_todo_not_a_failure(tmp_path: Path) -> None:
    report = run(tmp_path, facebook={"page_id": "PAGE", "access_token": ""})
    assert report.problems == [] and "paste the Page token into" in report.lines[0]


def test_a_good_token_is_confirmed_by_name_and_permissions(tmp_path: Path) -> None:
    fake = FakeGraph()
    fake.debug["expires_at"] = int(time.time()) + 60 * 86400
    report = run(tmp_path, fake)
    text = "\n".join(report.lines)
    assert report.problems == []
    assert "the Page 'Dhaka Kacchi' (id PAGE)" in text and "read the Page's posts" in text
    assert (
        "pages_manage_posts" in text and "expires in 59 days" in text
    ) or "expires in 60 days" in text
    assert not [r for r in fake.requests if r.method == "POST"]  # checking never posts


def test_without_the_app_secret_it_still_works_and_says_what_it_cannot_show(tmp_path: Path) -> None:
    report = run(tmp_path, app_secret="")
    assert report.problems == [] and any("add app_secret" in line for line in report.lines)


def test_a_token_missing_the_posting_permission_is_called_out(tmp_path: Path) -> None:
    fake = FakeGraph()
    fake.debug["scopes"] = ["pages_show_list", "pages_read_engagement"]
    report = run(tmp_path, fake)
    assert any("lacks permission: pages_manage_posts" in p for p in report.problems)


def test_a_user_token_is_refused_because_posting_needs_a_page_token(tmp_path: Path) -> None:
    fake = FakeGraph()
    fake.debug["type"] = "USER"
    assert any("USER token" in p and "me/accounts" in p for p in run(tmp_path, fake).problems)


def test_a_token_about_to_expire_is_flagged(tmp_path: Path) -> None:
    fake = FakeGraph()
    fake.debug["expires_at"] = int(time.time()) + 2 * 86400
    assert any("expires in" in p and "long-lived" in p for p in run(tmp_path, fake).problems)


def test_a_wrong_token_is_reported_with_facebooks_words(tmp_path: Path) -> None:
    report = run(tmp_path, facebook={"page_id": "PAGE", "access_token": "garbage"})
    assert any(
        "Facebook refused the token" in p and "Invalid OAuth access token" in p
        for p in report.problems
    )


def test_a_missing_page_id_is_a_failure(tmp_path: Path) -> None:
    assert run(tmp_path, facebook={"page_id": "", "access_token": "x"}).problems == [
        "facebook.page_id is empty in the credentials file"
    ]


def test_no_secret_ever_appears_in_the_report(tmp_path: Path) -> None:
    report = run(tmp_path)
    shown = "\n".join(report.lines + report.problems)
    assert TOKEN not in shown and "APPSECRET" not in shown


def test_check_meta_uses_the_configured_api_version(tmp_path: Path) -> None:
    fake = FakeGraph()
    check_meta(make(tmp_path), load_platforms(DEFAULT_PLATFORMS_CONFIG), fake.transport())
    assert all(r.url.path.startswith("/v25.0/") for r in fake.requests)
