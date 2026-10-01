from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dk_publishing.application.ports import Clock, PublisherRegistry, UnitOfWork


@dataclass(frozen=True, slots=True)
class Services:
    """Everything a use case needs, wired once in the composition root."""

    uow: Callable[[], UnitOfWork]  # a fresh unit of work (one connection, one transaction)
    publishers: PublisherRegistry
    clock: Clock
