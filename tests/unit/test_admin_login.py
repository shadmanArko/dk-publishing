"""Sign-in with the admin panel's login (ADR 0026): what the adapter asks, and what it trusts."""

from __future__ import annotations

import json

import httpx
import pytest

from dk_publishing.adapters.auth.admin_login import AdminBackendLogin, login_url_ok
from dk_publishing.adapters.config.review import admin_login_settings
from dk_publishing.application.ports import LoginUnavailable

URL = "https://api.example.test/v1/admin/login"


def backend(
    status: int = 200, email: str = "Owner@Example.test"
) -> tuple[AdminBackendLogin, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/logout"):
            return httpx.Response(204)
        body = {"token": "t-123", "adminUser": {"id": "1", "email": email, "name": "O"}}
        return httpx.Response(status, json=body if status == 200 else {"error": "x"})

    return AdminBackendLogin(URL, httpx.MockTransport(handle)), seen


def test_a_right_login_returns_the_admins_email_and_closes_the_session_it_opened() -> None:
    login, seen = backend()
    assert login.verify("owner@example.test", "pw") == "owner@example.test"
    assert [r.url.path for r in seen] == ["/v1/admin/login", "/v1/admin/logout"]
    assert json.loads(seen[0].content) == {"email": "owner@example.test", "password": "pw"}
    assert seen[1].headers["authorization"] == "Bearer t-123"


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_a_refused_login_is_a_plain_no(status: int) -> None:
    login, seen = backend(status)
    assert login.verify("owner@example.test", "nope") is None
    assert len(seen) == 1  # nothing to close


@pytest.mark.parametrize("status", [429, 500, 502, 404])
def test_an_unexpected_answer_is_never_a_yes(status: int) -> None:
    login, _ = backend(status)
    with pytest.raises(LoginUnavailable):
        login.verify("owner@example.test", "pw")


def test_an_unreachable_backend_is_unavailable_not_a_yes() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(LoginUnavailable):
        AdminBackendLogin(URL, httpx.MockTransport(down)).verify("a@b.test", "pw")


def test_an_answer_that_is_not_json_is_unavailable() -> None:
    login = AdminBackendLogin(
        URL, httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    )
    with pytest.raises(LoginUnavailable):
        login.verify("a@b.test", "pw")


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (URL, True),
        ("http://api.example.test/v1/admin/login", False),  # no plain http off this machine
        ("http://localhost:8787/v1/admin/login", True),
        ("https://api.example.test/v1/admin/orders", False),
        ("", False),
    ],
)
def test_only_an_https_login_address_is_accepted(url: str, ok: bool) -> None:
    assert login_url_ok(url) is ok


def test_sign_in_is_on_only_with_an_address_and_at_least_one_allowed_email() -> None:
    assert admin_login_settings({}) is None
    assert admin_login_settings({"REVIEW_ADMIN_LOGIN_URL": URL}) is None
    assert admin_login_settings({"REVIEW_ALLOWED_EMAILS": "a@b.test"}) is None
    assert (
        admin_login_settings(
            {"REVIEW_ADMIN_LOGIN_URL": "http://x.test/login", "REVIEW_ALLOWED_EMAILS": "a@b.test"}
        )
        is None
    )
    on = admin_login_settings(
        {"REVIEW_ADMIN_LOGIN_URL": URL, "REVIEW_ALLOWED_EMAILS": " A@B.test, c@d.test ,"}
    )
    assert on == (URL, frozenset({"a@b.test", "c@d.test"}))
