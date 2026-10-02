from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from dk_publishing import composition
from dk_publishing.adapters.config.platforms import ConfigError
from dk_publishing.adapters.notify.telegram import (
    TelegramCredentials,
    TelegramNotifier,
    ping_heartbeat,
    run_setup,
    write_template,
)
from dk_publishing.application.ports import NotifyError

TOKEN = "123456:ABC-secret-token-value"


class Bot:
    """A Telegram that answers from a script and remembers what it was asked."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def ok(result: object = True) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_a_message_is_posted_as_html_to_the_right_chat() -> None:
    bot = Bot(ok({}))
    TelegramNotifier(TOKEN, "42", transport=bot.transport).send("<b>hi</b>")
    [request] = bot.requests
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML" and body["text"] == "<b>hi</b>"


def test_long_text_is_cut_to_what_telegram_accepts() -> None:
    bot = Bot(ok({}))
    TelegramNotifier(TOKEN, "42", transport=bot.transport).send("x" * 5000)
    text = json.loads(bot.requests[0].content)["text"]
    assert len(text) == 4096 and text.endswith("…")


@pytest.mark.parametrize(
    ("reply", "words"),
    [
        (
            httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
            "refused the bot token",
        ),
        (httpx.Response(404, json={"ok": False}), "refused the bot token"),
        (
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}}),
            "retry in 7s",
        ),
        (
            httpx.Response(400, json={"ok": False, "description": "chat not found"}),
            "chat not found",
        ),
        (httpx.Response(502, text="<html>bad gateway"), "502"),
    ],
)
def test_telegram_errors_become_one_explained_failure(reply: httpx.Response, words: str) -> None:
    with pytest.raises(NotifyError, match=words):
        TelegramNotifier(TOKEN, "42", transport=Bot(reply).transport).send("x")


def test_a_network_failure_never_leaks_the_token() -> None:
    bot = Bot(
        httpx.ConnectError(f"cannot connect to https://api.telegram.org/bot{TOKEN}/sendMessage")
    )
    with pytest.raises(NotifyError) as caught:
        TelegramNotifier(TOKEN, "42", transport=bot.transport).send("x")
    assert TOKEN not in str(caught.value) and "ConnectError" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_template_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "d" / "telegram.json"
    assert write_template(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text('{"bot_token": "mine"}')
    assert not write_template(path) and "mine" in path.read_text()


def test_a_file_without_both_values_is_explained(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    write_template(path)
    with pytest.raises(ConfigError, match="needs bot_token and chat_id"):
        TelegramCredentials(path).notifier()
    path.write_text("{nope")
    with pytest.raises(ConfigError, match="not valid JSON"):
        TelegramCredentials(path).load()
    path.write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        TelegramCredentials(path).load()
    with pytest.raises(ConfigError, match="cannot read"):
        TelegramCredentials(tmp_path / "none.json").load()


def test_setup_init_then_chat_then_check_then_test(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    first = run_setup("init", path)
    assert first.ok and "@BotFather" in "\n".join(first.lines)
    assert "already exists" in run_setup("init", path).lines[0]

    empty = run_setup("check", path)
    assert not empty.ok and "bot_token is empty" in empty.lines[0]

    path.write_text(json.dumps({"bot_token": TOKEN, "chat_id": ""}))
    updates = ok(
        [
            {"message": {"chat": {"id": -100, "type": "group", "first_name": "g"}}},
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Shadman"}}},
        ]
    )
    found = run_setup("chat", path, transport=Bot(updates).transport)
    assert found.ok and "Shadman" in found.lines[0]
    assert json.loads(path.read_text())["chat_id"] == "42"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    bot = Bot(ok({"username": "dk_bot"}), ok({"username": "dk_bot"}), ok({}))
    assert run_setup("check", path, transport=bot.transport).lines == [
        "[ok]   the bot @dk_bot accepts the token"
    ]
    tested = run_setup("test", path, transport=bot.transport)
    assert tested.ok and "sent a test message" in tested.lines[-1]


def test_chat_with_no_messages_tells_you_what_to_do(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"bot_token": TOKEN}))
    result = run_setup("chat", path, transport=Bot(ok([])).transport)
    assert not result.ok and "send it 'hi'" in result.lines[0]


def test_setup_reports_a_bad_token_not_a_traceback(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"bot_token": TOKEN, "chat_id": "1"}))
    result = run_setup("check", path, transport=Bot(httpx.Response(401, json={})).transport)
    assert not result.ok and TOKEN not in "".join(result.lines)


def test_the_heartbeat_ping_never_raises() -> None:
    assert ping_heartbeat("https://hc.example/ping", transport=Bot(httpx.Response(200)).transport)
    assert not ping_heartbeat(
        "https://hc.example/ping", transport=Bot(httpx.Response(500)).transport
    )
    assert not ping_heartbeat(
        "https://hc.example/ping", transport=Bot(httpx.ConnectError("x")).transport
    )


def test_without_configuration_there_is_no_notifier_and_no_heartbeat() -> None:
    assert composition.build_notifier({}) is None
    assert composition.ping_alive({}) is None
    assert composition.credential_warnings({}) == []


def test_a_configured_notifier_is_built_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"bot_token": TOKEN, "chat_id": "1"}))
    assert composition.build_notifier({"TELEGRAM_CREDENTIALS_FILE": str(path)}) is not None
    path.write_text(json.dumps({"bot_token": "", "chat_id": ""}))
    with pytest.raises(ConfigError):
        composition.build_notifier({"TELEGRAM_CREDENTIALS_FILE": str(path)})


def test_an_expiring_token_becomes_a_digest_warning(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    path = tmp_path / "meta.json"
    soon = (datetime.now(UTC) + timedelta(days=3, hours=1)).isoformat()
    path.write_text(json.dumps({"threads": {"access_token": "t", "expires_at": soon}}))
    [warning] = composition.credential_warnings({"META_CREDENTIALS_FILE": str(path)})
    assert "expires in 3 days" in warning and "meta-refresh" in warning
    later = (datetime.now(UTC) + timedelta(days=40)).isoformat()
    path.write_text(json.dumps({"threads": {"access_token": "t", "expires_at": later}}))
    assert composition.credential_warnings({"META_CREDENTIALS_FILE": str(path)}) == []
    gone = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    path.write_text(json.dumps({"threads": {"access_token": "t", "expires_at": gone}}))
    assert "has expired" in composition.credential_warnings({"META_CREDENTIALS_FILE": str(path)})[0]
