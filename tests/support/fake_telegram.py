"""A Telegram that records what it was sent, and can be told to fail."""

from __future__ import annotations

from dk_publishing.application.ports import NotifyError


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.videos: list[tuple[str, str]] = []  # (file name, caption)
        self.down = False

    def send(self, text: str) -> None:
        if self.down:
            raise NotifyError("could not reach Telegram (ConnectError)")
        self.sent.append(text)

    def send_video(self, path: str, caption: str) -> None:
        if self.down:
            raise NotifyError("could not reach Telegram (ConnectError)")
        self.videos.append((path.rsplit("/", 1)[-1], caption))
