"""Connect to Google as the service account and report, in plain words, what it can reach.

Nothing here prints or logs the key. Problems are phrased for the person fixing the setup.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
)
FIXED_TABS = ("README", "Posts", "Calendar", "_lists")
_REQUIRED_KEY_FIELDS = ("client_email", "private_key", "token_uri")


class CredentialsError(Exception):
    """The key file is missing or is not a usable service-account key."""


@dataclass
class AccessReport:
    service_account_email: str = ""
    key_file_warning: str | None = None
    sheet_title: str | None = None
    sheet_tabs: list[str] = field(default_factory=list)
    can_edit_sheet: bool | None = None
    folder_name: str | None = None
    folder_files: int | None = None
    folder_files_truncated: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def load_service_account(path: Path) -> tuple[service_account.Credentials, str, str | None]:
    """Returns (credentials, client_email, warning). Never returns or logs the private key."""
    if not path.is_file():
        raise CredentialsError(f"no key file at {path}. Check GOOGLE_APPLICATION_CREDENTIALS.")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CredentialsError(f"{path} is not a JSON file. Download the key as JSON.") from None
    if not isinstance(data, dict):
        raise CredentialsError(f"{path} is not a service-account key.")
    if data.get("type") != "service_account":
        kind = "an OAuth client file" if "installed" in data or "web" in data else "another kind"
        raise CredentialsError(
            f"{path} is {kind} of Google credential, not a service-account key. In Google Cloud "
            "go to IAM & Admin > Service Accounts > your account > Keys > Add key > JSON."
        )
    missing = [k for k in _REQUIRED_KEY_FIELDS if not data.get(k)]
    if missing:
        raise CredentialsError(f"the key file is incomplete (missing {', '.join(missing)}).")
    try:
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            data, scopes=SCOPES
        )
    except ValueError:
        raise CredentialsError(
            "the private key inside the file is damaged; download a new one."
        ) from None

    warning = None
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warning = f"{path} can be read by other users on this computer; run: chmod 600 {path}"
    return credentials, str(data["client_email"]), warning


def check_access(
    sheets: Any, drive: Any, *, sheet_id: str, folder_id: str, email: str
) -> AccessReport:
    """Probe the Sheet and the Drive folder with already-built API clients."""
    report = AccessReport(service_account_email=email)

    try:
        meta = (
            sheets.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="properties.title,sheets.properties.title")
            .execute()
        )
        report.sheet_title = meta["properties"]["title"]
        report.sheet_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
    except HttpError as exc:
        report.problems.append(
            _explain(exc, what="the Google Sheet", item_id=sheet_id, email=email, role="Editor")
        )

    try:
        caps = (
            drive.files()
            .get(fileId=sheet_id, fields="capabilities/canEdit", supportsAllDrives=True)
            .execute()
        )
        report.can_edit_sheet = bool(caps["capabilities"]["canEdit"])
        if not report.can_edit_sheet:
            report.problems.append(
                f"The service account can only view the Sheet. Share it with {email} as Editor, "
                "so status can be written back."
            )
    except HttpError:
        pass  # already explained by the Sheets call above, or Drive is covered below

    try:
        info = (
            drive.files()
            .get(fileId=folder_id, fields="name,mimeType", supportsAllDrives=True)
            .execute()
        )
        report.folder_name = info["name"]
        if info["mimeType"] != "application/vnd.google-apps.folder":
            report.problems.append(
                "GOOGLE_DRIVE_FOLDER_ID is a file, not a folder. "
                "Open the folder and copy the ID from its URL."
            )
        listing = (
            drive.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken,files(id)",
                pageSize=100,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        report.folder_files = len(listing.get("files", []))
        report.folder_files_truncated = "nextPageToken" in listing
    except HttpError as exc:
        report.problems.append(
            _explain(
                exc, what="the Drive media folder", item_id=folder_id, email=email, role="Viewer"
            )
        )

    return report


def expected_missing_tabs(report: AccessReport, platform_tabs: Sequence[str]) -> list[str]:
    wanted = [*FIXED_TABS, *platform_tabs]
    return [tab for tab in wanted if tab not in report.sheet_tabs]


def connect(path: Path) -> tuple[Any, Any, str, str | None]:
    """Build Sheets and Drive clients. Returns (sheets, drive, email, key_file_warning)."""
    credentials, email, warning = load_service_account(path)
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return sheets, drive, email, warning


def _explain(exc: HttpError, *, what: str, item_id: str, email: str, role: str) -> str:
    status = getattr(exc.resp, "status", None)
    text = str(exc).lower()
    if "has not been used" in text or "is disabled" in text or "accessnotconfigured" in text:
        api = "Google Drive API" if "drive" in text else "Google Sheets API"
        return (
            f"The {api} is switched off for this Google Cloud project. "
            "Enable it under APIs & Services > Library."
        )
    if status == 404:
        return (
            f"Cannot find {what} (ID {item_id}). Either the ID is wrong, or it is not shared with "
            f"{email}. Share it with that address as {role}."
        )
    if status == 403:
        return f"{email} is not allowed to open {what}. Share it with that address as {role}."
    if status in (400, 401):
        return (
            f"Google rejected the key for {what} ({status}). Download a fresh JSON key for {email}."
        )
    return f"Unexpected Google error for {what}: HTTP {status}."
