from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from google.oauth2 import service_account
from googleapiclient.errors import HttpError
from httplib2 import Response  # type: ignore[import-untyped]

from dk_publishing import composition
from dk_publishing.adapters.sheets.google_access import (
    AccessReport,
    CredentialsError,
    check_access,
    expected_missing_tabs,
    load_service_account,
)
from dk_publishing.entrypoints import cli

EMAIL = "dk-publishing@dk-publishing.iam.gserviceaccount.com"
SECRET = "-----BEGIN PRIVATE KEY-----TOPSECRETMATERIAL"
FOLDER_MIME = "application/vnd.google-apps.folder"


def http_error(status: int, message: str = "") -> HttpError:
    body = json.dumps({"error": {"message": message}}).encode()
    return HttpError(Response({"status": status}), body)


class Call:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result, self.error = result, error

    def execute(self) -> Any:
        if self.error:
            raise self.error
        return self.result


class FakeSheets:
    def __init__(self, tabs: list[str] | None = None, error: Exception | None = None) -> None:
        self.tabs, self.error = tabs if tabs is not None else ["Posts"], error

    def spreadsheets(self) -> FakeSheets:
        return self

    def get(self, **_: Any) -> Call:
        meta = {
            "properties": {"title": "DK Social Planner"},
            "sheets": [{"properties": {"title": t}} for t in self.tabs],
        }
        return Call(meta, self.error)


class FakeDrive:
    """Spells the accessor `files()` like googleapiclient does."""

    def __init__(
        self,
        *,
        can_edit: bool = True,
        folder_mime: str = FOLDER_MIME,
        folder_error: Exception | None = None,
        item_count: int = 3,
        more: bool = False,
    ) -> None:
        self.can_edit, self.folder_mime, self.folder_error = can_edit, folder_mime, folder_error
        self.item_count, self.more = item_count, more

    def files(self) -> FakeDrive:
        return self

    def get(self, *, fileId: str, **_: Any) -> Call:
        if fileId == "SHEET":
            return Call({"capabilities": {"canEdit": self.can_edit}})
        return Call({"name": "DK media", "mimeType": self.folder_mime}, self.folder_error)

    def list(self, **_: Any) -> Call:
        listing: dict[str, Any] = {"files": [{"id": str(i)} for i in range(self.item_count)]}
        if self.more:
            listing["nextPageToken"] = "x"
        return Call(listing)


def probe(sheets: FakeSheets | None = None, drive: FakeDrive | None = None) -> AccessReport:
    return check_access(
        sheets or FakeSheets(),
        drive or FakeDrive(),
        sheet_id="SHEET",
        folder_id="FOLDER",
        email=EMAIL,
    )


def test_everything_reachable_is_reported_ok() -> None:
    report = probe(FakeSheets(["README", "Posts"]))
    assert report.ok
    assert (report.sheet_title, report.sheet_tabs, report.can_edit_sheet) == (
        "DK Social Planner",
        ["README", "Posts"],
        True,
    )
    assert (report.folder_name, report.folder_files, report.folder_files_truncated) == (
        "DK media",
        3,
        False,
    )


def test_a_long_folder_listing_says_there_is_more() -> None:
    assert probe(drive=FakeDrive(item_count=100, more=True)).folder_files_truncated


def test_view_only_access_to_the_sheet_is_a_problem_naming_the_fix() -> None:
    [problem] = probe(drive=FakeDrive(can_edit=False)).problems
    assert EMAIL in problem and "Editor" in problem


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (404, "", "not shared with"),
        (403, "", "is not allowed to open"),
        (403, "Google Sheets API has not been used in project 1 before", "Google Sheets API"),
        (400, "", "Download a fresh JSON key"),
        (500, "", "Unexpected Google error"),
    ],
)
def test_sheet_errors_are_explained(status: int, message: str, expected: str) -> None:
    report = probe(FakeSheets(error=http_error(status, message)))
    assert any(expected in p for p in report.problems), report.problems
    assert not report.ok


def test_a_folder_that_is_not_shared_names_the_account_and_the_role() -> None:
    report = probe(drive=FakeDrive(folder_error=http_error(404)))
    [problem] = report.problems
    assert EMAIL in problem and "FOLDER" in problem and "Viewer" in problem


def test_a_disabled_drive_api_is_named() -> None:
    err = http_error(
        403, "Google Drive API has not been used in project 1 before or it is disabled"
    )
    [problem] = probe(drive=FakeDrive(folder_error=err)).problems
    assert "Google Drive API" in problem and "APIs & Services" in problem


def test_pasting_a_file_id_where_a_folder_id_belongs_is_caught() -> None:
    [problem] = probe(drive=FakeDrive(folder_mime="application/pdf")).problems
    assert "is a file, not a folder" in problem


def test_missing_tabs_are_listed_but_are_not_failures() -> None:
    report = probe(FakeSheets(["README", "Posts", "Instagram"]))
    missing = expected_missing_tabs(report, ["Instagram", "TikTok"])
    assert missing == ["Calendar", "_lists", "TikTok"] and report.ok


# the key file -----------------------------------------------------------------------------------


def write_key(tmp_path: Path, content: object, mode: int = 0o600) -> Path:
    path = tmp_path / "key.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    path.chmod(mode)
    return path


GOOD_KEY = {
    "type": "service_account",
    "client_email": EMAIL,
    "private_key": SECRET,
    "token_uri": "https://oauth2.googleapis.com/token",
}


@pytest.fixture
def fake_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        classmethod(lambda cls, info, scopes=None: object()),
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "not a JSON file"),
        ("[]", "not a service-account key"),
        ({"installed": {"client_id": "x"}}, "OAuth client file"),
        ({"type": "authorized_user"}, "another kind"),
        ({"type": "service_account", "client_email": EMAIL}, "missing private_key, token_uri"),
    ],
)
def test_wrong_files_are_explained(tmp_path: Path, content: object, message: str) -> None:
    with pytest.raises(CredentialsError, match=message):
        load_service_account(write_key(tmp_path, content))


def test_a_missing_file_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match=r"no key file at .*nope\.json"):
        load_service_account(tmp_path / "nope.json")


def test_a_good_key_loads_and_a_private_file_gets_no_warning(
    tmp_path: Path, fake_credentials: None
) -> None:
    _, email, warning = load_service_account(write_key(tmp_path, GOOD_KEY, 0o600))
    assert email == EMAIL and warning is None


def test_a_world_readable_key_gets_a_chmod_hint(tmp_path: Path, fake_credentials: None) -> None:
    path = write_key(tmp_path, GOOD_KEY, 0o644)
    _, _, warning = load_service_account(path)
    assert warning is not None and "chmod 600" in warning


def test_a_damaged_private_key_is_reported_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(cls: object, info: object, scopes: object = None) -> None:
        raise ValueError(f"could not parse {SECRET}")

    monkeypatch.setattr(
        service_account.Credentials, "from_service_account_info", classmethod(broken)
    )
    with pytest.raises(CredentialsError) as caught:
        load_service_account(write_key(tmp_path, GOOD_KEY))
    assert "damaged" in str(caught.value) and "TOPSECRET" not in str(caught.value)


# the command ------------------------------------------------------------------------------------


def set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_SHEET_ID", "GOOGLE_DRIVE_FOLDER_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_the_command_lists_what_is_missing_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_env(monkeypatch, GOOGLE_SHEET_ID="s")
    assert cli.main(["check-google"]) == 2
    err = capsys.readouterr().err
    assert "GOOGLE_APPLICATION_CREDENTIALS" in err and "GOOGLE_DRIVE_FOLDER_ID" in err


def test_the_command_reports_a_bad_key_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    set_env(
        monkeypatch,
        GOOGLE_APPLICATION_CREDENTIALS=str(tmp_path / "missing.json"),
        GOOGLE_SHEET_ID="s",
        GOOGLE_DRIVE_FOLDER_ID="f",
    )
    assert cli.main(["check-google"]) == 1
    assert "[FAIL] no key file" in capsys.readouterr().err


@pytest.mark.parametrize(("problems", "code"), [([], 0), (["Share the Sheet"], 1)])
def test_the_command_prints_a_readable_summary_and_sets_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    problems: list[str],
    code: int,
) -> None:
    report = AccessReport(
        service_account_email=EMAIL,
        key_file_warning="chmod 600 it",
        sheet_title="DK Social Planner",
        sheet_tabs=["Posts"],
        can_edit_sheet=True,
        folder_name="DK media",
        folder_files=2,
        problems=problems,
    )
    set_env(
        monkeypatch,
        GOOGLE_APPLICATION_CREDENTIALS="~/key.json",
        GOOGLE_SHEET_ID="s",
        GOOGLE_DRIVE_FOLDER_ID="f",
    )
    monkeypatch.setattr(composition, "check_google", lambda *a: (report, ["Calendar"]))

    assert cli.main(["check-google"]) == code
    out = capsys.readouterr().out
    assert f"key loaded for {EMAIL}" in out and "[warn] chmod 600 it" in out
    assert (
        "'DK Social Planner': 1 tabs, can edit" in out and "tabs not created yet: Calendar" in out
    )
    assert "'DK media': 2 items" in out
    assert ("[FAIL] Share the Sheet" in out) == bool(problems)
    assert os.path.expanduser("~") not in out  # paths are never echoed back
