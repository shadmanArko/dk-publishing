"""What the use cases need from the outside world, as Protocols. Adapters implement them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from dk_publishing.domain.attempt import Outcome, Phase
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.model import Variant, VariantEvent
from dk_publishing.domain.planning import Action, NextStep
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation
from dk_publishing.domain.sheet import MediaFile, RawRow, SheetSnapshot, StatusPlan
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


@runtime_checkable
class NativeScheduler(Protocol):
    """A platform that can hold a post and publish it at a future time by itself.

    `schedule` is a write that cannot be undone by retrying it: an uncertain outcome leaves a
    scheduled post that this system cannot see, so it is failed for a person to check, never
    retried. `cancel` must succeed quietly if the scheduled post is already gone.
    """

    def schedule(
        self, snapshot: VariantSnapshot, media: Sequence[Rendition], at: datetime
    ) -> Handle: ...

    def cancel(self, handle: Handle) -> None: ...


class PublisherRegistry(Protocol):
    def for_platform(self, platform: str) -> Publisher: ...


class MediaStore(Protocol):
    def ensure_local(self, media: Sequence[Mapping[str, Any]]) -> list[Rendition]:
        """Make each approved media file available on local disk, verified against the checksum
        it had when approved. Safe to repeat; a file already downloaded is reused."""


class MediaPreview(Protocol):
    def url_for(self, item: Mapping[str, Any]) -> str | None:
        """A temporary link the owner's browser can show for one media item of a post (the same
        tokenised links platforms fetch from), or None when there is no way to show it."""


class DuplicateAccount(Exception):
    """An account with this display name (or external id) already exists on the platform."""


@dataclass(frozen=True, slots=True)
class AccountInfo:
    id: str
    platform: str
    display_name: str | None
    status: str


@dataclass(frozen=True, slots=True)
class SyncVariant:
    """A variant with the context the Sheet sync needs to match it to a row."""

    variant: Variant
    post_key: str
    title: str | None
    account_name: str | None
    source_hash: str | None
    external_url: str | None
    external_id: str | None
    last_reason: str | None  # the reason on the variant's latest event
    source: str = "human"  # who made the post: 'human' (the Sheet) or 'agent' (the API)
    content: Mapping[str, Any] | None = None  # the frozen snapshot, or an API draft's submission
    created_by: str | None = None  # the API key that made it


class SheetGateway(Protocol):
    def read(self) -> SheetSnapshot:
        """Read the Posts and platform tabs into typed rows. Must be called before `write`."""

    def write(self, plan: StatusPlan) -> int:
        """Write status back in one batch. Returns how many cells were written."""


class MediaCatalog(Protocol):
    def list_files(self) -> list[MediaFile]: ...


class DuplicateKey(Exception):
    """An API key with this name is already in use."""


@dataclass(frozen=True, slots=True)
class ApiKey:
    id: str
    tenant_id: str
    name: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    name: str
    scopes: tuple[str, ...]
    created_at: datetime
    last_used_at: datetime | None
    revoked: bool


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    id: str
    filename: str
    sha256: str
    size: int
    purged: bool = False  # the file was removed from the server after its retention period


@dataclass(frozen=True, slots=True)
class StoredPost:
    id: str
    post_key: str
    label: str | None
    source: str
    request_hash: str | None
    created_by: str | None


class DuplicatePost(Exception):
    """A post with this post_key already exists."""


class ApiRepository(Protocol):
    def add_key(self, tenant_id: str, name: str, key_hash: str, scopes: Sequence[str]) -> str:
        """Returns the key id. Raises DuplicateKey."""

    def key_by_hash(self, key_hash: str) -> ApiKey | None:
        """The key, unless it was revoked."""

    def keys(self, tenant_id: str) -> list[ApiKeyInfo]: ...

    def revoke_key(self, tenant_id: str, name: str, at: datetime) -> bool: ...

    def touch_key(self, key_id: str, at: datetime) -> None: ...

    def add_media(
        self, tenant_id: str, filename: str, sha256: str, size: int, created_by: str
    ) -> str:
        """Returns the media id."""

    def media(self, tenant_id: str, ids: Sequence[str]) -> dict[str, UploadedMedia]:
        """The known ones among `ids`, by id."""

    def media_bytes(self, tenant_id: str, created_by: str) -> int:
        """Bytes this key has uploaded that are still on the server."""

    def media_since(self, tenant_id: str, since: datetime) -> list[UploadedMedia]:
        """Uploads made at or after `since` (a file waiting for its post is not unused yet)."""

    def mark_media_purged(self, tenant_id: str, sha256s: Sequence[str], at: datetime) -> int:
        """Record that these files were removed from the disk. Returns how many rows changed."""

    def post(self, tenant_id: str, post_key: str) -> StoredPost | None: ...

    def create_post(
        self, tenant_id: str, post_key: str, label: str | None, request_hash: str, created_by: str
    ) -> str:
        """A new post with source 'agent'. Returns its id. Raises DuplicatePost."""

    def posts(self, tenant_id: str, limit: int) -> list[StoredPost]:
        """Posts made through the API, newest first."""

    def save_content(self, tenant_id: str, variant_id: str, content: Mapping[str, Any]) -> None: ...

    def content(self, variant_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    id: str
    key_id: str
    key_name: str
    url: str
    events: tuple[str, ...]  # empty: every event
    active: bool
    disabled_reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """One message to send (or one already sent, when read back for the owner)."""

    id: str
    endpoint_id: str
    url: str
    secret: str
    event_id: str
    event_type: str
    payload: Mapping[str, Any]
    attempts: int
    status: str = "pending"
    last_status: int | None = None
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    delivered_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WebhookCandidate:
    """A state change an endpoint has not been told about yet."""

    endpoint_id: str
    variant_id: str
    seq: int
    event_type: str
    at: datetime
    post_key: str
    platform: str
    account: str
    format: str | None
    status: str
    slot: datetime
    live_url: str | None
    reason: str | None
    handed_over: bool


class WebhookRepository(Protocol):
    def add_endpoint(
        self, tenant_id: str, key_id: str, url: str, secret: str, events: Sequence[str]
    ) -> WebhookEndpoint: ...

    def endpoints(self, tenant_id: str, key_id: str | None = None) -> list[WebhookEndpoint]:
        """All endpoints, or one key's. Newest first. The secret is never returned."""

    def endpoint_secret(
        self, tenant_id: str, key_id: str, endpoint_id: str
    ) -> tuple[str, str] | None:
        """(url, secret) of one of the key's own live endpoints, for a test message."""

    def delete_endpoint(self, tenant_id: str, key_id: str, endpoint_id: str) -> bool: ...

    def disable_endpoint(self, endpoint_id: str, reason: str) -> bool: ...

    def candidates(self, tenant_id: str, since: datetime, limit: int) -> list[WebhookCandidate]:
        """Events of live API posts that a live endpoint of the creating key should hear about,
        newer than both `since` and the endpoint itself, and not yet queued."""

    def queue(
        self,
        tenant_id: str,
        candidate: WebhookCandidate,
        event_id: str,
        payload: Mapping[str, Any],
        at: datetime,
    ) -> bool:
        """Queue one message. False if it was already queued (the producer may run twice)."""

    def claim_due(self, now: datetime, limit: int, lease: timedelta) -> list[WebhookDelivery]:
        """Messages due now, leased for `lease` so a second worker cannot send them at the same
        time. A claimed message that is never resolved becomes due again after the lease."""

    def has_due(self, now: datetime) -> bool: ...

    def record_attempt(
        self,
        delivery_id: str,
        at: datetime,
        *,
        ok: bool,
        http_status: int | None,
        error: str | None,
        retry_at: datetime | None,
    ) -> None:
        """Success: delivered. Failure: pending again at `retry_at`, or dead when it is None."""

    def deliveries(
        self, tenant_id: str, key_id: str, endpoint_id: str, limit: int
    ) -> list[WebhookDelivery]: ...


class WebhookUrlRejected(Exception):
    """A webhook address that must not be called. `code` is stable, `message` is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    http_status: int | None = None
    error: str | None = None  # short, never contains the address or any part of the body
    gone: bool = False  # HTTP 410: the client says it will never listen again


class WebhookSender(Protocol):
    def check(self, url: str) -> None:
        """Raises WebhookUrlRejected unless the address is https and only public hosts answer it."""

    def send(
        self,
        url: str,
        secret: str,
        *,
        delivery_id: str,
        event_id: str,
        event_type: str,
        body: bytes,
        now: datetime,
    ) -> SendResult:
        """One signed POST, no redirects. Never raises for a failed delivery: it returns it."""


class DuplicateAttempt(Exception):
    """An attempt with this idempotency key already exists. The call must not be made again."""


@dataclass(frozen=True, slots=True)
class DueAction:
    """A variant whose next action is due. The run key is `<action>:<variant_id>:<version>`."""

    variant_id: str
    action: Action
    at: datetime
    version: int
    platform: str
    account_id: str  # runs are limited to one at a time per account


class SyncRepository(Protocol):
    def accounts(self, tenant_id: str) -> list[AccountInfo]: ...

    def add_account(
        self, tenant_id: str, platform: str, display_name: str, external_id: str
    ) -> str:
        """Returns the new account id. Raises DuplicateAccount."""

    def upsert_post(self, tenant_id: str, post_key: str, title: str) -> str:
        """Returns the post id, creating the post or updating its title."""

    def variants(self, tenant_id: str) -> list[SyncVariant]: ...

    def update_inputs(self, variant_id: str, *, publish_at: datetime, source_hash: str) -> bool:
        """Record new inputs on a draft or invalid variant without a state change. False if the
        variant is no longer one of those."""

    def save_snapshots(self, tenant_id: str, sync_id: str, rows: Sequence[RawRow]) -> None: ...


class VariantRepository(Protocol):
    def add(self, variant: Variant, *, source_hash: str | None = None) -> None:
        """Insert a new variant. It must be a fresh draft (version 0, no snapshot)."""

    def get(self, variant_id: str) -> Variant | None: ...

    def snapshot_of(self, variant_id: str) -> Mapping[str, Any] | None:
        """The frozen content, so the publish step can re-hash it against `snapshot_hash`."""

    def handle_of(self, variant_id: str) -> Handle | None:
        """The adapter's handle stored by `apply(..., handle=...)`."""

    def events(self, variant_id: str) -> list[VariantEvent]: ...

    def stale(self, status: VariantStatus, updated_before: datetime, limit: int) -> list[Variant]:
        """Variants that have sat in `status` since before `updated_before`."""

    def media_in_use(self, final_since: datetime) -> list[Mapping[str, Any]]:
        """The media items (as frozen or submitted) of every variant that may still need its
        files: anything not in a final state, and anything that became final at or after
        `final_since`. Includes the content of API drafts, which have no snapshot yet."""

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
        source_hash: str | None = None,
    ) -> bool:
        """Persist one transition atomically, as a compare-and-set on (status, version).

        Returns False, writing nothing, when another run already moved the variant: the caller
        lost the race and must not act. `snapshot` is required exactly when this transition
        freezes one, and its hash must match `new.snapshot_hash`. `handle` stores the adapter's
        handle; `live` records the platform id and URL on the move to published. Going back to
        draft clears the handle.
        """

    def due(self, now: datetime, limit: int) -> list[DueAction]: ...

    def last_change(self) -> str:
        """The latest time any variant changed state, as text ("" if there are none)."""


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


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A variant moving into a state a person must hear about (failed, expired, uncertain)."""

    key: str
    variant_id: str
    title: str
    platform: str
    account: str
    status: str
    reason: str
    at: datetime
    publish_at: datetime


@dataclass(frozen=True, slots=True)
class DigestItem:
    title: str
    platform: str
    account: str
    status: str
    publish_at: datetime
    published_at: datetime | None
    reason: str | None


class LoginUnavailable(Exception):
    """The sign-in service could not be asked (down, slow, or answered something unexpected)."""


class AdminLogin(Protocol):
    """Checks an email and password against the admin panel's own login (ADR 0026). This service
    never stores, logs or forwards a password anywhere else."""

    def verify(self, email: str, password: str) -> str | None:
        """The admin's email when the pair is right, None when it is wrong.
        Raises LoginUnavailable when no answer could be had (never treat that as a yes)."""


class NotifyError(Exception):
    """The message could not be delivered. Nothing is marked as sent, so it is tried again."""


class Notifier(Protocol):
    def send(self, text: str) -> None:
        """Deliver `text` (Telegram HTML) to the owner, or raise NotifyError."""

    def send_video(self, path: str, caption: str) -> None:
        """Deliver a video file with a short caption, or raise NotifyError."""

    def send_photos(self, paths: Sequence[str], caption: str) -> None:
        """Deliver one or more images (as one album) with a short caption, or raise NotifyError."""


class AlertRepository(Protocol):
    def unsent_events(self, tenant_id: str, since: datetime, limit: int) -> list[AlertEvent]: ...

    def mark_sent(self, tenant_id: str, key: str, now: datetime) -> bool:
        """Record a delivery. False if it was already recorded."""

    def was_sent(self, tenant_id: str, key: str) -> bool: ...

    def results(self, tenant_id: str, start: datetime, end: datetime) -> list[DigestItem]:
        """Variants that reached published, failed or expired with a slot in [start, end)."""

    def upcoming(self, tenant_id: str, start: datetime, end: datetime) -> list[DigestItem]:
        """Approved or held variants with a slot in [start, end), earliest first."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    filename: str  # a safe file name that keeps the extension
    sha256: str
    size: int


class IntakeRejected(Exception):
    """An uploaded or fetched file was refused. `code` is stable, `message` is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DiskUsage:
    total: int
    used: int

    @property
    def percent(self) -> int:
        return 0 if self.total <= 0 else int(self.used * 100 / self.total)


@dataclass(frozen=True, slots=True)
class RemovedFile:
    name: str
    size: int
    sha256: str | None  # set for API uploads, so their records can be marked as removed


class MediaDisk(Protocol):
    """The disk holding uploaded and downloaded media."""

    def usage(self) -> DiskUsage: ...

    def delete_unused(
        self, keep: Sequence[Mapping[str, Any]], modified_before: datetime, *, dry_run: bool = False
    ) -> list[RemovedFile]:
        """Delete every media file that no item in `keep` refers to and that was last touched
        before `modified_before` (and any abandoned half-written file). With `dry_run`, only
        report what would go."""


class UploadWriter(Protocol):
    def write(self, chunk: bytes) -> None:
        """Raises IntakeRejected when the file is too big."""

    def finish(self) -> StoredFile: ...

    def abort(self) -> None: ...


class MediaIntake(Protocol):
    """Where files sent through the API are kept, so a post can name them by id."""

    def begin(self, filename: str, limit: int | None = None) -> UploadWriter:
        """Raises IntakeRejected if the file type is not one any platform takes. `limit` lowers
        the size cap for this one file (what is left of the uploader's quota)."""

    def fetch(self, url: str, filename: str | None, limit: int | None = None) -> StoredFile:
        """Download a public https file. Raises IntakeRejected."""


class UnitOfWork(Protocol):
    @property
    def api(self) -> ApiRepository: ...

    @property
    def webhooks(self) -> WebhookRepository: ...

    @property
    def alerts(self) -> AlertRepository: ...

    @property
    def variants(self) -> VariantRepository: ...

    @property
    def attempts(self) -> AttemptRepository: ...

    @property
    def sync(self) -> SyncRepository: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Roll back anything not committed, then release the connection."""

    def commit(self) -> None: ...
