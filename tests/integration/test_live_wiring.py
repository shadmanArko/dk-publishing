"""Switching a platform to `live` is a deliberate act: these tests pin down what it takes."""

from __future__ import annotations

from pathlib import Path

import pytest

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.platforms.dry_run import DryRunPublisher
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.registry import UnknownPlatform
from tests.support.fake_graph import FakeGraph


def write_config(tmp_path: Path, facebook: str, extra: str = "") -> Path:
    path = tmp_path / "platforms.yaml"
    path.write_text(f"platforms:\n  facebook: {{mode: {facebook}, api_version: v25.0}}\n{extra}")
    return path


LIVE_ENV = {
    "FACEBOOK_PAGE_ID": "PAGE",
    "FACEBOOK_PAGE_TOKEN_FILE": "/nonexistent/token",  # only read when a call is made
    "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/key.json",
}


@pytest.fixture(autouse=True)
def no_google(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building the media store connects to Drive; there is no Google in these tests."""
    monkeypatch.setattr(composition, "connect", lambda path: (None, object(), "sa@x", None))


def test_the_shipped_config_has_exactly_one_live_platform_and_the_rest_cannot_post(
    conninfo: str, tmp_path: Path
) -> None:
    env = {**LIVE_ENV, "META_CREDENTIALS_FILE": str(make_credentials(tmp_path))}
    services = composition.build_services(conninfo, env=env)
    assert isinstance(services.publishers.for_platform("facebook"), FacebookPublisher)
    for name in ("instagram", "threads", "youtube", "tiktok", "linkedin"):
        assert isinstance(services.publishers.for_platform(name), DryRunPublisher), name


def make_credentials(tmp_path: Path) -> Path:
    path = tmp_path / "meta.json"
    path.write_text(
        '{"facebook": {"page_id": "PAGE", "access_token": "x"},'
        ' "threads": {"user_id": "TH1", "access_token": "y"}}'
    )
    return path


def test_live_facebook_builds_the_real_adapter_and_a_media_store(
    conninfo: str, tmp_path: Path
) -> None:
    services = composition.build_services(
        conninfo, write_config(tmp_path, "live"), env={**LIVE_ENV, "MEDIA_DIR": str(tmp_path / "m")},
        transport=FakeGraph().transport(),
    )  # fmt: skip
    publisher = services.publishers.for_platform("facebook")
    assert isinstance(publisher, FacebookPublisher)
    assert services.media_store is not None
    assert publisher.capabilities.native_window is None  # published by us at the slot, for now


def test_other_platforms_stay_dry_run_next_to_a_live_one(conninfo: str, tmp_path: Path) -> None:
    config = write_config(tmp_path, "live", "  instagram: {mode: dry_run}\n  x: {mode: off}\n")
    services = composition.build_services(conninfo, config, env=LIVE_ENV)
    assert isinstance(services.publishers.for_platform("instagram"), DryRunPublisher)
    with pytest.raises(UnknownPlatform):
        services.publishers.for_platform("x")


@pytest.mark.parametrize("missing", ["FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_TOKEN_FILE"])
def test_a_live_platform_without_its_credentials_stops_start_up_naming_the_variable(
    conninfo: str, tmp_path: Path, missing: str
) -> None:
    env = {k: v for k, v in LIVE_ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        composition.build_services(conninfo, write_config(tmp_path, "live"), env=env)


def test_a_live_platform_needs_google_to_fetch_its_media(conninfo: str, tmp_path: Path) -> None:
    env = {k: v for k, v in LIVE_ENV.items() if k != "GOOGLE_APPLICATION_CREDENTIALS"}
    with pytest.raises(ConfigError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        composition.build_services(conninfo, write_config(tmp_path, "live"), env=env)


def test_a_platform_marked_live_without_an_adapter_is_refused(
    conninfo: str, tmp_path: Path
) -> None:
    config = tmp_path / "platforms.yaml"
    config.write_text("platforms:\n  instagram: {mode: live}\n")
    with pytest.raises(ConfigError, match="no such adapter is built yet"):
        composition.build_services(conninfo, config, env=LIVE_ENV)


def test_a_live_account_carries_the_platforms_own_id(conninfo: str) -> None:
    composition.add_account(conninfo, "facebook", "Dhaka Kacchi", external_id="1234567890")
    [account] = composition.list_accounts(conninfo)
    assert account.display_name == "Dhaka Kacchi"
    import psycopg

    with psycopg.connect(conninfo) as conn:
        row = conn.execute("SELECT external_id FROM publishing.social_accounts").fetchone()
    assert row == ("1234567890",)
