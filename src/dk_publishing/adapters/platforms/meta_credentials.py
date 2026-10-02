"""One file for everything Meta: app id, app secret, and an id plus token per product.

Kept outside the repository. It is read fresh on every call, so pasting a new token needs no
restart. The encrypted token vault replaces it when the connect flow exists.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.secrets_file import SecretsFile
from dk_publishing.domain.errors import AuthFailed

DEFAULT_PATH = Path("~/.config/dk-publishing/meta.json")
HELP = (
    "Keep this file OUTSIDE the git repository and never share it. Paste each token between "
    "the quotes. app_secret is optional; with it `dk meta check` can show what a token allows."
)


class MetaCredentials:
    def __init__(self, path: Path | str) -> None:
        self._file = SecretsFile(path, what="Meta credentials")
        self.path = self._file.path

    def _load(self) -> dict[str, Any]:
        return self._file.read()

    def section(self, name: str) -> Mapping[str, Any]:
        section = self._load().get(name)
        if not isinstance(section, dict):
            raise ConfigError(f"{self.path} has no '{name}' section; run `dk init`")
        return section

    def update_section(self, name: str, changes: Mapping[str, Any]) -> None:
        """Change fields of one section and write the file back atomically, keeping it private.
        Everything else in the file is preserved."""
        data = self._load()
        if not isinstance(data.get(name), dict):
            raise ConfigError(f"{self.path} has no '{name}' section; run `dk init`")
        self._file.update({name: {**data[name], **changes}})

    def value(self, key: str) -> str:
        return str(self._load().get(key) or "").strip()


class SectionTokenProvider:
    """The `access_token` of one section, re-read on every call.

    With `fallback`, a section whose token is empty uses another section's. Instagram publishing
    through Facebook Login uses the Facebook Page token, so one token can serve both.
    """

    def __init__(
        self, credentials: MetaCredentials, section: str, fallback: str | None = None
    ) -> None:
        self._credentials = credentials
        self._section = section
        self._fallback = fallback

    def token(self) -> str:
        try:
            value = str(self._credentials.section(self._section).get("access_token") or "").strip()
            if not value and self._fallback:
                value = str(
                    self._credentials.section(self._fallback).get("access_token") or ""
                ).strip()
        except ConfigError as exc:
            raise AuthFailed(str(exc)) from None
        if not value:
            raise AuthFailed(
                f"no access_token for '{self._section}' in {self._credentials.path}; paste one in"
            )
        return value


def write_template(
    path: Path, *, app_id: str, page_id: str, instagram_id: str, threads_id: str
) -> bool:
    """Create the file with owner-only permissions. Returns False, writing nothing, if it exists."""
    path = path.expanduser()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "_help": HELP,
        "app_id": app_id,
        "app_secret": "",
        "facebook": {"page_id": page_id, "access_token": ""},
        "instagram": {"account_id": instagram_id, "access_token": ""},
        "threads": {"user_id": threads_id, "access_token": ""},
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(template, handle, indent=2)
        handle.write("\n")
    return True


def write_template_from_env(path: Path, env: Mapping[str, str]) -> bool:
    """Pre-fill the template with the ids (never tokens) named in the environment."""
    return write_template(
        path,
        app_id=env.get("META_APP_ID", ""),
        page_id=env.get("FACEBOOK_PAGE_ID", ""),
        instagram_id=env.get("INSTAGRAM_ACCOUNT_ID", ""),
        threads_id=env.get("THREADS_USER_ID", ""),
    )
