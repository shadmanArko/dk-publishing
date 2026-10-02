"""A Telegram that records what it was sent, and can be told to fail."""

from __future__ import annotations

from dk_publishing.application.ports import NotifyError


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.down = False

    def send(self, text: str) -> None:
        if self.down:
            raise NotifyError("could not reach Telegram (ConnectError)")
        self.sent.append(text)
