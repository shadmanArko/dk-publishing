"""What the use cases need from the outside world, as Protocols. Adapters implement them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self

from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.model import Variant, VariantEvent
from dk_publishing.domain.planning import Action, NextStep


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

    def events(self, variant_id: str) -> list[VariantEvent]: ...

    def apply(
        self,
        old: Variant,
        new: Variant,
        event: VariantEvent,
        next_step: NextStep | None,
        *,
        snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """Persist one transition atomically, as a compare-and-set on (status, version).

        Returns False, writing nothing, when another run already moved the variant: the caller
        lost the race and must not act. `snapshot` is required exactly when this transition
        freezes one, and its hash must match `new.snapshot_hash`.
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
