from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from dk_publishing.application.ports import (
    Clock,
    MediaCatalog,
    MediaDisk,
    MediaIntake,
    MediaPreview,
    MediaStore,
    Notifier,
    PublisherRegistry,
    SheetGateway,
    UnitOfWork,
    WebhookSender,
)
from dk_publishing.domain.guide import PlatformGuide
from dk_publishing.domain.spec import PlatformFacts


@dataclass(frozen=True, slots=True)
class Services:
    """Everything a use case needs, wired once in the composition root."""

    uow: Callable[[], UnitOfWork]  # a fresh unit of work (one connection, one transaction)
    publishers: PublisherRegistry
    clock: Clock
    media_store: MediaStore | None = None  # None: platforms get no files (dry-run)


@dataclass(frozen=True, slots=True)
class SyncServices:
    """What the Sheet sync needs on top of the publishing services."""

    core: Services
    sheet: SheetGateway
    media: MediaCatalog
    platforms: Sequence[str]  # every platform key, so empty account lists get cleared too
    tenant_id: str = "dk"
    max_cancellations: int = 5  # more scheduled cancellations than this in one sync is a slip
    review_base_url: str = ""  # the review page's public address; "" = no `watch` links (ADR 0025)


@dataclass(frozen=True, slots=True)
class ApiServices:
    """What the HTTP API needs: the publishing services plus the platform guides and file intake."""

    core: Services
    guides: Mapping[str, PlatformGuide]  # only the platforms that are switched on
    modes: Mapping[str, str]  # platform -> live | assisted | dry_run
    intake: MediaIntake
    notifier: Notifier | None = None
    tenant_id: str = "dk"
    disk: MediaDisk | None = None  # None: the disk is not watched (tests)
    key_quota_bytes: int = 0  # most one key may keep on the server; 0 = no limit
    disk_stop_percent: int = 85  # uploads are refused from this fill level
    webhooks: WebhookSender | None = None  # None: webhooks are off
    facts: Mapping[str, PlatformFacts] = field(
        default_factory=dict
    )  # route and quotas per platform
    max_upload_bytes: int = 0  # largest file the API takes; 0 = not stated
    upload_types: tuple[str, ...] = ()  # file extensions the API takes


@dataclass(frozen=True, slots=True)
class ReviewServices:
    """What the owner's daily review page needs. It is only ever built when a review secret and a
    public address are configured; without them the page does not exist."""

    core: Services
    secret: str  # signs links, sessions and form tokens; never logged
    base_url: str  # the public address the links point at
    tenant_id: str = "dk"
    preview: MediaPreview | None = None  # None: media is named but not shown
