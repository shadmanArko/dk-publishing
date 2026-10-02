"""Telegram delivery, the credentials file and the one-time setup (`dk telegram ...`).

The bot token is part of every request URL, so no exception or log line may carry a URL: every
failure is turned into a short message that names the problem, never the address.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.config.secrets_file import SecretsFile, split_ref
from dk_publishing.application.ports import NotifyError

API = "https://api.telegram.org"
DEFAULT_PATH = Path("~/.config/dk-publishing/telegram.json")
MAX_TEXT = 4096
TIMEOUT = httpx.Timeout(10.0)


class TelegramCredentials:
    def __init__(self, path: Path | str) -> None:
        self._file = SecretsFile(path, what="Telegram credentials")
        self.path = self._file.path

    def load(self) -> dict[str, Any]:
        return self._file.read()

    def update(self, values: Mapping[str, Any]) -> None:
        self._file.update(values)

    def notifier(self, transport: httpx.BaseTransport | None = None) -> TelegramNotifier:
        data = self.load()
        token, chat = (
            str(data.get("bot_token") or "").strip(),
            str(data.get("chat_id") or "").strip(),
        )
        if not token or not chat:
            raise ConfigError(
                f"{self.path} needs bot_token and chat_id; run `make telegram-init` for the steps"
            )
        return TelegramNotifier(token, chat, transport=transport)


def write_template(path: Path) -> bool:
    """Create the credentials file with owner-only permissions. False if it exists."""
    path = path.expanduser()
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "_help": (
            "Keep this file OUTSIDE the git repository. Telegram > @BotFather > /newbot, paste "
            "the token below, send your new bot any message, then run `make telegram-chat`; it "
            "fills in chat_id."
        ),
        "bot_token": "",
        "chat_id": "",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(template, handle, indent=2)
        handle.write("\n")
    return True


def _call(
    token: str, method: str, payload: Mapping[str, Any], transport: httpx.BaseTransport | None
) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            response = client.post(f"{API}/bot{token}/{method}", json=dict(payload))
    except httpx.HTTPError as exc:
        raise NotifyError(f"could not reach Telegram ({type(exc).__name__})") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 200 and body.get("ok"):
        result = body.get("result")
        return result if isinstance(result, dict) else {"items": result}
    description = str(body.get("description") or response.reason_phrase)[:200]
    if response.status_code == 429:
        wait = (body.get("parameters") or {}).get("retry_after")
        raise NotifyError(f"Telegram asked us to slow down (retry in {wait}s)")
    if response.status_code in (401, 404):
        raise NotifyError("Telegram refused the bot token; check bot_token in the credentials file")
    raise NotifyError(f"Telegram said {response.status_code}: {description}")


class TelegramNotifier:
    def __init__(
        self, token: str, chat_id: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._token, self._chat, self._transport = token, chat_id, transport

    def send(self, text: str) -> None:
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        _call(
            self._token,
            "sendMessage",
            {
                "chat_id": self._chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            self._transport,
        )

    def identity(self) -> str:
        return str(_call(self._token, "getMe", {}, self._transport).get("username") or "?")


_STEPS = (
    "1. In Telegram open @BotFather, send /newbot and follow the questions.\n"
    "2. Paste the token it gives you into telegram.bot_token in {file}.\n"
    "3. Open your new bot and send it any message (for example: hi).\n"
    "4. Run: make telegram-chat"
)


@dataclass
class SetupResult:
    ok: bool
    lines: list[str]


def run_setup(
    command: str, path: Path | str, *, transport: httpx.BaseTransport | None = None
) -> SetupResult:
    """`init` makes the file, `chat` finds your chat id, `check` tests the bot, `test` messages."""
    credentials = TelegramCredentials(path)
    if command == "init":
        if split_ref(path)[1] is not None:
            return SetupResult(True, [_STEPS.format(file=credentials.path)])
        if write_template(credentials.path):
            return SetupResult(
                True,
                [f"[did]  created {credentials.path} (owner-only, outside the repo)", _STEPS],
            )
        return SetupResult(True, [f"{credentials.path} already exists; left it alone"])
    data = credentials.load()
    token = str(data.get("bot_token") or "").strip()
    if not token:
        return SetupResult(False, [f"[FAIL] bot_token is empty in {credentials.path}"])
    try:
        if command == "chat":
            return _find_chat(credentials, token, transport)
        notifier = credentials.notifier(transport)
        lines = [f"[ok]   the bot @{notifier.identity()} accepts the token"]
        if command == "test":
            notifier.send("✅ <b>DK Publishing</b>: Telegram alerts are connected.")
            lines.append("[ok]   sent a test message; check your Telegram")
        return SetupResult(True, lines)
    except (NotifyError, ConfigError) as exc:
        return SetupResult(False, [f"[FAIL] {exc}"])


def _find_chat(
    credentials: TelegramCredentials, token: str, transport: httpx.BaseTransport | None
) -> SetupResult:
    result = _call(token, "getUpdates", {"limit": 20}, transport)
    chats: dict[str, str] = {}
    for update in result.get("items") or []:
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private" and "id" in chat:
            chats[str(chat["id"])] = str(chat.get("first_name") or chat.get("username") or "you")
    if not chats:
        return SetupResult(
            False, ["[FAIL] no messages found; open your bot, send it 'hi', then run this again"]
        )
    chat_id, name = list(chats.items())[-1]
    credentials.update({"chat_id": chat_id})
    return SetupResult(True, [f"[ok]   saved the chat with {name}", "Now run: make telegram-test"])


def ping_heartbeat(url: str, *, transport: httpx.BaseTransport | None = None) -> bool:
    """Tell the external dead-man's-switch monitor we are alive. Never raises; False on failure."""
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=True) as client:
            return client.get(url).is_success
    except httpx.HTTPError:
        return False
