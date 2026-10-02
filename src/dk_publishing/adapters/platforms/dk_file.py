"""The single configuration file, `dk.json`: every id, key and token the system needs.

It lives outside the repository, next to the Google key file. Only `DATABASE_URL` and
`DK_CONFIG_FILE` (where this file is) come from the environment. `resolve_env` turns the file into
the environment names the rest of the code already reads, so an explicitly set environment
variable still wins (handy for one-off overrides and older setups).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.secrets_file import make_ref

DEFAULT_PATH = Path("~/.config/dk-publishing/dk.json")
ENV_NAME = "DK_CONFIG_FILE"

HELP = (
    "The ONE place for every id, key and token. Keep this folder outside git and never share it. "
    "Fill in only what you use; leave the rest empty. Setup guide: README.md. "
    "Check your work any time with: make check-setup"
)

TEMPLATE: dict[str, Any] = {
    "_help": HELP,
    "google": {
        "_help": "Sheet + Drive access. docs/setup/google.md",
        "service_account_file": "google-key.json",
        "sheet_id": "",
        "drive_folder_id": "",
    },
    "meta": {
        "_help": "Facebook, Instagram, Threads. docs/setup/meta.md",
        "app_id": "",
        "app_secret": "",
        "facebook": {"page_id": "", "access_token": ""},
        "instagram": {"account_id": "", "access_token": ""},
        "threads": {"user_id": "", "access_token": ""},
    },
    "youtube": {
        "_help": "docs/setup/youtube.md. `make connect-youtube` fills refresh_token and channel_id",
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "channel_id": "",
    },
    "telegram": {
        "_help": "Alerts and the morning digest. docs/setup/telegram.md",
        "bot_token": "",
        "chat_id": "",
    },
    "alerts": {
        "_help": "Optional dead-man's switch: a Healthchecks.io-style ping URL",
        "heartbeat_url": "",
    },
    "media": {
        "_help": (
            "Only for Instagram/Threads photos: where this server shares files publicly. "
            "docs/setup/meta.md"
        ),
        "public_base_url": "",
        "public_dir": "",
        "work_dir": "",
    },
}

# dk.json location -> the environment name the rest of the code reads
_VALUES = {
    ("google", "sheet_id"): "GOOGLE_SHEET_ID",
    ("google", "drive_folder_id"): "GOOGLE_DRIVE_FOLDER_ID",
    ("alerts", "heartbeat_url"): "HEARTBEAT_URL",
    ("media", "public_base_url"): "PUBLIC_MEDIA_BASE_URL",
    ("media", "public_dir"): "PUBLIC_MEDIA_DIR",
    ("media", "work_dir"): "MEDIA_DIR",
}
_SECTIONS = {
    "meta": "META_CREDENTIALS_FILE",
    "youtube": "YOUTUBE_CREDENTIALS_FILE",
    "telegram": "TELEGRAM_CREDENTIALS_FILE",
}


def config_path(env: Mapping[str, str]) -> Path | None:
    value = env.get(ENV_NAME, "").strip()
    return Path(value).expanduser() if value else None


def configured_accounts(env: Mapping[str, str]) -> list[tuple[str, str]]:
    """(platform, the platform's own account id) for each account dk.json has an id for."""
    path = config_path(env)
    if path is None:
        return []
    data = load(path)
    meta, youtube = data.get("meta") or {}, data.get("youtube") or {}
    found = [
        ("facebook", (meta.get("facebook") or {}).get("page_id")),
        ("instagram", (meta.get("instagram") or {}).get("account_id")),
        ("threads", (meta.get("threads") or {}).get("user_id")),
        ("youtube", youtube.get("channel_id")),
    ]
    return [(platform, str(ident).strip()) for platform, ident in found if str(ident or "").strip()]


def create_template(path: Path) -> bool:
    """Write the empty template, owner-only. False (and nothing written) if the file exists."""
    path = path.expanduser()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(TEMPLATE, handle, indent=2)
        handle.write("\n")
    return True


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except OSError:
        raise ConfigError(
            f"{ENV_NAME} points to {path}, which cannot be read; run `make init`"
        ) from None
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    return data


def resolve_env(env: Mapping[str, str]) -> dict[str, str]:
    """`env` plus whatever dk.json provides. Without DK_CONFIG_FILE, `env` is returned unchanged."""
    resolved = dict(env)
    path = config_path(env)
    if path is None:
        return resolved
    data = load(path)

    def fill(name: str, value: str) -> None:
        if value and not resolved.get(name, "").strip():
            resolved[name] = value

    for (section, key), name in _VALUES.items():
        fill(name, str((data.get(section) or {}).get(key) or "").strip())
    key_file = str((data.get("google") or {}).get("service_account_file") or "").strip()
    if key_file:
        fill("GOOGLE_APPLICATION_CREDENTIALS", str((path.parent / key_file).expanduser()))
    for section, name in _SECTIONS.items():
        if isinstance(data.get(section), dict):
            fill(name, make_ref(path, section))
    return resolved
