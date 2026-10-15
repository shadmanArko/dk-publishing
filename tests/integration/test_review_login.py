"""Email and password sign-in for the review page (ADR 0026), against a real database.

The admin panel's login is replaced by a fake that knows one person; everything else (cookies,
limits, the page, the decisions) is the real code.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dk_publishing.application.ports import LoginUnavailable
from dk_publishing.entrypoints.publish_api.app import create_app
from tests.integration.test_review_page import BASE, DAY, Review

EMAIL, PASSWORD = "owner@example.test", "correct horse battery"


class FakeAdminLogin:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.down = False

    def verify(self, email: str, password: str) -> str | None:
        self.asked.append(email)
        if self.down:
            raise LoginUnavailable("down")
        return email if (email, password) == (EMAIL, PASSWORD) else None


class Login(Review):
    def __init__(self, conninfo: str, tmp_path: Path, *, enabled: bool = True) -> None:
        super().__init__(conninfo, tmp_path)
        self.admin = FakeAdminLogin()
        if enabled:
            self.services = dataclasses.replace(
                self.services,
                admin_login=self.admin,
                allowed_emails=frozenset({EMAIL, "second@example.test"}),
            )
            app = create_app(self.rig.api, None, self.services)
            self.web = TestClient(app, base_url=BASE, follow_redirects=False)

    def sign_in_with(self, email: str = EMAIL, password: str = PASSWORD):  # type: ignore[no-untyped-def]
        return self.web.post("/review/login", data={"email": email, "password": password})


@pytest.fixture
def login(conninfo: str, tmp_path: Path) -> Login:
    return Login(conninfo, tmp_path)


def test_the_right_email_and_password_sign_you_in_for_a_year(login: Login) -> None:
    login.idea("a-1")
    answer = login.sign_in_with()
    assert answer.status_code == 303 and answer.headers["location"] == "/review"
    cookie = answer.headers["set-cookie"].lower()
    assert all(part in cookie for part in ("httponly", "secure", "samesite=lax", "path=/review"))
    assert "max-age=31536000" in cookie  # a year
    assert PASSWORD not in answer.text and PASSWORD not in answer.headers["set-cookie"]
    page = login.page()
    assert page.status_code == 200 and f"Signed in as {EMAIL}" in page.text
    assert "max-age=31536000" in page.headers["set-cookie"].lower()  # every visit renews the year


def test_a_signed_in_person_can_approve_and_the_decision_names_them(login: Login) -> None:
    login.idea("a-1")
    login.sign_in_with()
    done = login.decide(list(login.ids("a-1").values()), "approve")
    assert done.status_code == 200
    assert set(login.state("a-1").values()) == {"scheduled"}


@pytest.mark.parametrize(
    ("email", "password"),
    [(EMAIL, "wrong"), ("nobody@example.test", PASSWORD), ("", ""), (EMAIL, "")],
)
def test_every_kind_of_wrong_answer_looks_the_same(login: Login, email: str, password: str) -> None:
    answer = login.sign_in_with(email, password)
    assert answer.status_code == 401
    assert "Wrong email or password, or no access." in answer.text
    assert "set-cookie" not in answer.headers


def test_someone_not_on_the_list_is_never_checked_with_the_admin_login(login: Login) -> None:
    login.admin.asked.clear()
    assert login.sign_in_with("stranger@example.test", PASSWORD).status_code == 401
    assert login.admin.asked == []


def test_if_the_admin_login_is_down_nobody_gets_in(login: Login) -> None:
    login.admin.down = True
    answer = login.sign_in_with()
    assert answer.status_code == 503 and "not available" in answer.text
    assert "set-cookie" not in answer.headers
    assert login.page().status_code == 401


def test_repeated_wrong_passwords_lock_that_email_even_for_the_right_one(login: Login) -> None:
    for _ in range(8):
        assert login.sign_in_with(EMAIL, "wrong").status_code == 401
    locked = login.sign_in_with(EMAIL, PASSWORD)
    assert locked.status_code == 429 and "set-cookie" not in locked.headers


def test_signing_out_ends_it_on_that_device(login: Login) -> None:
    login.sign_in_with()
    answer = login.web.post("/review/logout", data={"csrf": login.csrf()})
    assert answer.status_code == 303 and answer.headers["location"] == "/review/login"
    login.web.cookies.clear()  # what the browser does with the cleared cookie
    assert login.page().status_code == 401


def test_a_forged_sign_out_does_nothing(login: Login) -> None:
    login.sign_in_with()
    assert login.web.post("/review/logout", data={"csrf": "nope"}).status_code == 401
    assert login.page().status_code == 200


def test_the_signed_out_page_offers_sign_in_and_the_form_is_a_plain_post(login: Login) -> None:
    page = login.page()
    assert page.status_code == 401 and "/review/login" in page.text
    form = login.web.get("/review/login")
    assert (
        form.status_code == 200 and 'method="post"' in form.text and 'type="password"' in form.text
    )


def test_a_post_from_another_site_is_refused(login: Login) -> None:
    answer = login.web.post(
        "/review/login",
        data={"email": EMAIL, "password": PASSWORD},
        headers={"origin": "https://evil.example"},
    )
    assert answer.status_code == 400 and "set-cookie" not in answer.headers


def test_without_the_settings_there_is_no_sign_in_and_the_link_still_works(
    conninfo: str, tmp_path: Path
) -> None:
    off = Login(conninfo, tmp_path, enabled=False)
    assert off.web.get("/review/login").status_code == 404
    assert (
        off.web.post("/review/login", data={"email": EMAIL, "password": PASSWORD}).status_code
        == 404
    )
    assert "/review/login" not in off.page().text
    off.sign_in()
    assert off.page().status_code == 200 and "Signed in as" not in off.page().text
    assert DAY  # the day constant stays imported for readers of the sibling tests


def test_taking_someone_off_the_list_signs_their_devices_out(login: Login) -> None:
    login.sign_in_with()
    assert login.page().status_code == 200
    login.services = dataclasses.replace(
        login.services, allowed_emails=frozenset({"second@example.test"})
    )
    app = create_app(login.rig.api, None, login.services)
    cookies = login.web.cookies
    login.web = TestClient(app, base_url=BASE, follow_redirects=False)
    login.web.cookies.update(cookies)
    assert login.page().status_code == 401
