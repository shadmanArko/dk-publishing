"""Signed review links, sessions and form tokens (ADR 0025): what they accept and what they refuse."""

from __future__ import annotations

import json
import logging
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.review import review_settings
from dk_publishing.adapters.platforms.dk_file import create_template
from dk_publishing.adapters.platforms.setup_check import check_setup
from dk_publishing.domain import review as r
from dk_publishing.entrypoints import cli
from dk_publishing.entrypoints.publish_api.review import HideLinkToken

SECRET = "s" * 43
NOW = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)


def good(purpose: str = r.LINK, ttl: timedelta = r.LINK_TTL, secret: str = SECRET) -> str:
    return r.issue(secret, purpose, NOW, ttl)


def verdict(
    token: str, purpose: str = r.LINK, secret: str = SECRET, at: datetime = NOW
) -> r.Verdict:
    return r.check(secret, token, purpose, at)[0]


# --- links and sessions -----------------------------------------------------------------------


def test_a_fresh_link_is_accepted_and_carries_its_purpose_and_end() -> None:
    state, claims = r.check(SECRET, good(), r.LINK, NOW + timedelta(hours=1))
    assert state is r.Verdict.OK and claims is not None
    assert claims.purpose == r.LINK and claims.expires == NOW + r.LINK_TTL


def test_a_link_expires_after_24_hours_and_says_expired_not_invalid() -> None:
    token = good()
    assert verdict(token, at=NOW + r.LINK_TTL - timedelta(seconds=1)) is r.Verdict.OK
    assert verdict(token, at=NOW + r.LINK_TTL) is r.Verdict.EXPIRED


def test_a_tampered_token_is_invalid_wherever_it_was_touched() -> None:
    token = good()
    payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    forged_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    for bad in (
        f"{payload}.{flipped}",  # signature changed
        f"{forged_payload}.{signature}",  # payload changed, old signature
        f"{payload}.",  # signature removed
        f".{signature}",  # payload removed
        payload,
        f"{token}.extra",
        "",
        "not a token",
        "a" * 600,
    ):
        assert verdict(bad) is r.Verdict.INVALID, bad


def test_an_extended_expiry_cannot_be_forged_without_the_secret() -> None:
    payload, signature = good().split(".")
    other = r.issue(SECRET, r.LINK, NOW + timedelta(days=30), r.LINK_TTL).split(".")[0]
    assert verdict(f"{other}.{signature}") is r.Verdict.INVALID
    assert payload != other


def test_a_token_signed_with_another_secret_is_invalid() -> None:
    assert verdict(good(secret="x" * 43)) is r.Verdict.INVALID


def test_a_session_cookie_is_not_a_link_and_a_link_is_not_a_session() -> None:
    session = good(r.SESSION, r.SESSION_TTL)
    assert verdict(session, r.LINK) is r.Verdict.INVALID  # wrong purpose, though genuinely signed
    assert verdict(good(), r.SESSION) is r.Verdict.INVALID
    assert verdict(session, r.SESSION) is r.Verdict.OK


def test_an_expired_token_of_the_wrong_purpose_is_still_just_invalid() -> None:
    old = r.issue(SECRET, r.SESSION, NOW - timedelta(days=3), r.SESSION_TTL)
    assert verdict(old, r.LINK) is r.Verdict.INVALID


def test_every_token_is_different_and_url_safe() -> None:
    first, second = good(), good()
    assert first != second
    assert all(c.isalnum() or c in "-_." for c in first)


def test_a_form_token_belongs_to_one_session() -> None:
    one = r.check(SECRET, good(r.SESSION, r.SESSION_TTL), r.SESSION, NOW)[1]
    two = r.check(SECRET, good(r.SESSION, r.SESSION_TTL), r.SESSION, NOW)[1]
    assert one is not None and two is not None
    token = r.csrf_token(SECRET, one)
    assert r.csrf_ok(SECRET, one, token)
    assert not r.csrf_ok(SECRET, two, token)  # another session's form token
    assert not r.csrf_ok(SECRET, one, "")
    assert not r.csrf_ok("y" * 43, one, token)
    assert token != good(r.SESSION, r.SESSION_TTL)  # a form token is not a usable cookie


def test_the_secret_must_be_long_enough() -> None:
    assert r.secret_is_usable(r.new_secret()) and len(r.new_secret()) >= r.MIN_SECRET_LENGTH
    assert not r.secret_is_usable("short") and not r.secret_is_usable(" " * 40)


# --- addresses --------------------------------------------------------------------------------


def test_addresses_use_the_configured_base_and_never_a_trailing_slash() -> None:
    link = r.link_url("https://publish.example.test/", "TOKEN", date(2026, 11, 16))
    assert link == "https://publish.example.test/review/enter?t=TOKEN&day=2026-11-16"
    assert r.link_url("https://publish.example.test", "T").endswith("/review/enter?t=T")
    watch = r.watch_url("https://publish.example.test", date(2026, 11, 16), "2026w47-ig-mon1300-s1")
    assert watch == "https://publish.example.test/review?day=2026-11-16#i-2026w47-ig-mon1300-s1"
    assert "t=" not in watch  # the Sheet's link carries no credential


# --- failed attempts --------------------------------------------------------------------------


def test_the_tenth_forgery_in_ten_minutes_closes_the_door_for_that_source_only() -> None:
    limiter = r.FailureLimiter(limit=3, window=timedelta(minutes=10))
    for _ in range(2):
        limiter.record("a", NOW)
    assert not limiter.blocked("a", NOW)
    limiter.record("a", NOW)
    assert limiter.blocked("a", NOW) and not limiter.blocked("b", NOW)
    assert limiter.blocked("a", NOW + timedelta(minutes=9, seconds=59))
    assert not limiter.blocked("a", NOW + timedelta(minutes=10, seconds=1))  # forgiven


def test_the_limiter_forgets_a_flood_of_sources_instead_of_growing() -> None:
    limiter = r.FailureLimiter()
    for i in range(1500):
        limiter.record(f"source-{i}", NOW)
    assert not limiter.blocked("source-1499", NOW)  # reset, and still working


# --- the access log never holds a link --------------------------------------------------------


def test_the_access_log_filter_cuts_the_token_off_the_sign_in_address() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/review/enter?t=SECRETTOKEN&day=2026-11-16", "1.1", 303), None,
    )  # fmt: skip
    assert HideLinkToken().filter(record)
    assert "SECRETTOKEN" not in record.getMessage() and "/review/enter" in record.getMessage()
    other = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/review?day=2026-11-16", "1.1", 200), None,
    )  # fmt: skip
    HideLinkToken().filter(other)
    assert "/review?day=2026-11-16" in other.getMessage()


# --- configuration: on only with a usable secret and an https address -------------------------


def test_the_page_is_off_without_a_secret_and_off_with_a_bad_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = "https://publish.example.test"
    assert review_settings({}) is None
    assert review_settings({"REVIEW_BASE_URL": base}) is None
    with caplog.at_level(logging.WARNING, logger="dk.review"):
        assert review_settings({"REVIEW_SECRET": "short", "REVIEW_BASE_URL": base}) is None
        assert (
            review_settings({"REVIEW_SECRET": SECRET, "REVIEW_BASE_URL": "http://x.test"}) is None
        )
        assert review_settings({"REVIEW_SECRET": SECRET}) is None
    assert SECRET not in caplog.text and "short" not in caplog.text.replace("shorter", "")
    assert review_settings({"REVIEW_SECRET": SECRET, "REVIEW_BASE_URL": base + "/"}) == (
        SECRET,
        base,
    )
    local = {"REVIEW_SECRET": SECRET, "REVIEW_BASE_URL": "http://localhost:8000"}
    assert review_settings(local) is not None


def test_dk_json_feeds_the_review_environment(tmp_path: Path) -> None:
    path = tmp_path / "dk.json"
    create_template(path)
    data = json.loads(path.read_text())
    data["review"].update({"secret": SECRET, "public_base_url": "https://publish.example.test"})
    path.write_text(json.dumps(data))
    from dk_publishing.adapters.platforms.dk_file import resolve_env

    env = resolve_env({"DK_CONFIG_FILE": str(path)})
    assert env["REVIEW_SECRET"] == SECRET
    assert env["REVIEW_BASE_URL"] == "https://publish.example.test"


# --- `dk review init` and `dk review link` ----------------------------------------------------


@pytest.fixture
def dk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "conf" / "dk.json"
    create_template(path)
    monkeypatch.setenv("DK_CONFIG_FILE", str(path))
    monkeypatch.delenv("REVIEW_SECRET", raising=False)
    monkeypatch.delenv("REVIEW_BASE_URL", raising=False)
    return path


def test_init_makes_a_secret_in_dk_json_without_ever_printing_it(
    dk: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["review", "init", "--base-url", "https://publish.example.test/"]) == 0
    saved = json.loads(dk.read_text())["review"]
    shown = capsys.readouterr()
    assert r.secret_is_usable(saved["secret"])
    assert saved["public_base_url"] == "https://publish.example.test"
    assert saved["secret"] not in shown.out + shown.err
    assert stat.S_IMODE(dk.stat().st_mode) == 0o600


def test_init_keeps_an_existing_secret_unless_asked_to_rotate(dk: Path) -> None:
    env = {"DK_CONFIG_FILE": str(dk)}
    composition.init_review(env, "https://a.example.test")
    first = json.loads(dk.read_text())["review"]["secret"]
    assert composition.init_review(env, "https://b.example.test") == (dk, False)
    kept = json.loads(dk.read_text())["review"]
    assert kept["secret"] == first and kept["public_base_url"] == "https://b.example.test"
    assert composition.init_review(env, "https://b.example.test", rotate=True) == (dk, True)
    assert json.loads(dk.read_text())["review"]["secret"] != first


@pytest.mark.parametrize(
    "base", ["http://publish.example.test", "publish.example.test", "https://x/y"]
)
def test_init_refuses_an_address_that_cannot_work(dk: Path, base: str) -> None:
    with pytest.raises(ConfigError, match="--base-url"):
        composition.init_review({"DK_CONFIG_FILE": str(dk)}, base)


def test_the_link_command_prints_a_working_link_and_fails_plainly_when_off(
    dk: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["review", "link"]) == 1  # not set up yet
    assert "review page is off" in capsys.readouterr().err

    cli.main(["review", "init", "--base-url", "https://publish.example.test"])
    capsys.readouterr()
    assert cli.main(["review", "link", "--day", "2026-11-17"]) == 0
    link = capsys.readouterr().out.strip()
    assert link.startswith("https://publish.example.test/review/enter?t=")
    assert link.endswith("&day=2026-11-17")
    token = link.split("t=")[1].split("&")[0]
    secret = json.loads(dk.read_text())["review"]["secret"]
    assert r.check(secret, token, r.LINK, datetime.now(UTC))[0] is r.Verdict.OK
    assert cli.main(["review", "link", "--day", "tomorrow"]) == 2


def test_check_setup_reports_the_page_without_ever_failing_on_it(dk: Path) -> None:
    def lines(env: dict[str, str]) -> list[str]:
        return [ln for ln in check_setup(env, {}).lines if "review page" in ln]

    assert lines({"DK_CONFIG_FILE": str(dk)})[0].startswith("[skip]")
    data = json.loads(dk.read_text())
    data["review"]["secret"] = "hunter2"
    dk.write_text(json.dumps(data))
    [warn] = lines({"DK_CONFIG_FILE": str(dk)})
    assert warn.startswith("[warn]") and "hunter2" not in warn
    cli.main(["review", "init", "--base-url", "https://publish.example.test", "--rotate"])
    env = composition.environment()
    assert lines(env)[0].startswith("[ok]")
