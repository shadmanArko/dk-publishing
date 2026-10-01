"""Test doubles shared by the use-case, contract and rehearsal tests."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from dk_publishing.application.ports import Handle, Publisher
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

T = TypeVar("T")

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
H = timedelta(hours=1)

# Prepare-then-publish path, 2 hour deadline: what most platforms use.
CAPS = Capabilities(None, 30 * MIN, 24 * H, True, 2 * H)
# A platform that can also hold a post and publish it itself (10 minutes to 30 days ahead).
NATIVE_CAPS = Capabilities((10 * MIN, timedelta(days=30)), 5 * MIN, None, False, 2 * H)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SimulatedCrash(BaseException):
    """The process dies. A BaseException so no `except Exception` in the code under test sees it."""


class ScriptedPublisher:
    """Wraps a real publisher and misbehaves on cue, per method.

    Each script entry is consumed by one call: an exception instance is raised *before* the inner
    call (the platform never acted); `("after", exc)` runs the inner call first and then raises
    (the platform acted but the response was lost).
    """

    def __init__(
        self,
        inner: Publisher,
        *,
        prepare: Sequence[object] = (),
        publish: Sequence[object] = (),
        find_live: Sequence[object] = (),
        schedule: Sequence[object] = (),
        cancel: Sequence[object] = (),
    ) -> None:
        self.inner = inner
        self._scripts = {
            "prepare": deque(prepare),
            "publish": deque(publish),
            "find_live": deque(find_live),
            "schedule": deque(schedule),
            "cancel": deque(cancel),
        }
        self.calls = {"prepare": 0, "publish": 0, "find_live": 0, "schedule": 0, "cancel": 0}
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> Capabilities:
        return self.inner.capabilities

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        return self.inner.validate(snapshot)

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        return self._run("prepare", lambda: self.inner.prepare(snapshot, media))

    def publish(self, handle: Handle) -> LivePost:
        return self._run("publish", lambda: self.inner.publish(handle))

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        return self._run("find_live", lambda: self.inner.find_live(snapshot, handle))

    def schedule(
        self, snapshot: VariantSnapshot, media: Sequence[Rendition], at: datetime
    ) -> Handle:
        return self._run("schedule", lambda: self.inner.schedule(snapshot, media, at))  # type: ignore[attr-defined]

    def cancel(self, handle: Handle) -> None:
        self._run("cancel", lambda: self.inner.cancel(handle))  # type: ignore[attr-defined]

    def _run(self, method: str, call: Callable[[], T]) -> T:
        with self._lock:
            self.calls[method] += 1
            script = self._scripts[method]
            entry = script.popleft() if script else None
        if isinstance(entry, BaseException):
            raise entry
        result = call()
        if isinstance(entry, tuple) and entry[0] == "after":
            raise entry[1]
        return result
