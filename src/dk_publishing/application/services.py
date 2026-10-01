from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dk_publishing.application.ports import (
    Clock,
    MediaCatalog,
    PublisherRegistry,
    SheetGateway,
    UnitOfWork,
)


@dataclass(frozen=True, slots=True)
class Services:
    """Everything a use case needs, wired once in the composition root."""

    uow: Callable[[], UnitOfWork]  # a fresh unit of work (one connection, one transaction)
    publishers: PublisherRegistry
    clock: Clock


@dataclass(frozen=True, slots=True)
class SyncServices:
    """What the Sheet sync needs on top of the publishing services."""

    core: Services
    sheet: SheetGateway
    media: MediaCatalog
    platforms: Sequence[str]  # every platform key, so empty account lists get cleared too
    tenant_id: str = "dk"
    max_cancellations: int = 5  # more scheduled cancellations than this in one sync is a slip
