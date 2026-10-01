"""What the use cases need from the outside world, as Protocols. Adapters implement them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.model import Variant, VariantEvent
from dk_publishing.domain.planning import Action, NextStep
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation
from dk_publishing.domain.status import VariantStatus

Handle = Mapping[
    str, Any
]  # JSON an adapter keeps between prepare and publish (a container id, ...)


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware."""


class Publisher(Protocol):
    """One platform. Every adapter is substitutable; differences live in `capabilities`.

    Errors are raised as the domain's five outcomes. A call that may have reached the platform
    must raise UnknownOutcome, never Retryable: only the former is reconciled before any retry.
    """

    @property
    def capabilities(self) -> Capabilities: ...

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]: ...

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        """Upload or create containers ahead of the slot. Safe to repeat: it posts nothing."""

    def publish(self, handle: Handle) -> LivePost:
        """Make the post live. Called at most once per attempt, never blindly retried."""

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        """Ask the platform whether the post exists. None means confirmed not live; if the
        platform cannot answer, raise instead of returning None."""


class PublisherRegistry(Protocol):
    def for_platform(self, platform: str) -> Publisher: ...


class DuplicateAttempt(Exception):
    """An attempt with this idempotency key already exists. The call must not be made again."""


@dataclass(frozen=True, slots=True)
class DueAction:
    """A variant whose next action is due. The run key is `<action>:<variant_id>:<version>`."""

    variant_id: str
    action: Action
    at: datetime
    version: int


class VariantRepository(Protocol):
    def add(self, variant: Variant) -> None:
        """Insert a new variant. It must be a fresh draft (version 0, no snapshot)."""

    def get(self, variant_id: str) -> Variant | None: ...

    def snapshot_of(self, variant_id: str) -> Mapping[str, Any] | None:
        """The frozen content, so the publish step can re-hash it against `snapshot_hash`."""

    def handle_of(self, variant_id: str) -> Handle | None:
        """The adapter's handle stored by `apply(..., handle=...)`."""

    def events(self, variant_id: str) -> list[VariantEvent]: ...

    def stale(self, status: VariantStatus, updated_before: datetime, limit: int) -> list[Variant]:
        """Variants that have sat in `status` since before `updated_before`."""

    def apply(
        self,
        old: Variant,
        new: Variant,
        event: VariantEvent,
        next_step: NextStep | None,
        *,
        snapshot: Mapping[str, Any] | None = None,
        handle: Handle | None = None,
        live: LivePost | None = None,
    ) -> bool:
        """Persist one transition atomically, as a compare-and-set on (status, version).

        Returns False, writing nothing, when another run already moved the variant: the caller
        lost the race and must not act. `snapshot` is required exactly when this transition
        freezes one, and its hash must match `new.snapshot_hash`. `handle` stores the adapter's
        handle; `live` records the platform id and URL on the move to published. Going back to
        draft clears the handle.
        """

    def due(self, now: datetime, limit: int) -> list[DueAction]: ...


class AttemptRepository(Protocol):
    def begin(
        self,
        *,
        tenant_id: str,
        variant_id: str,
        phase: Phase,
        idempotency_key: str,
        started_at: datetime,
    ) -> str:
        """Record the intent to call a platform. Must be committed before the call is made.

        Raises DuplicateAttempt if the key was used before.
        """

    def finish(
        self,
        attempt_id: str,
        *,
        outcome: Outcome,
        finished_at: datetime,
        error_code: str | None = None,
        http_status: int | None = None,
        response_excerpt: str | None = None,
    ) -> None: ...

    def finish_open(
        self, variant_id: str, *, outcome: Outcome, finished_at: datetime, error_code: str
    ) -> int:
        """Complete attempts that never reported back (the run died). Returns how many."""

    def failures(self, variant_id: str, phase: Phase) -> int:
        """Finished attempts in `phase` that retryable, rate-limited or uncertain outcomes ended."""


class UnitOfWork(Protocol):
    @property
    def variants(self) -> VariantRepository: ...

    @property
    def attempts(self) -> AttemptRepository: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Roll back anything not committed, then release the connection."""

    def commit(self) -> None: ...
