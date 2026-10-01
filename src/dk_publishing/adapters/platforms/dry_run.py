"""The recording stand-in for any platform: everything real except the final call.

Used to rehearse a full week of Sheet rows before go-live, and for every new adapter. Its "posts"
go to a ledger that behaves like the platform would: a second publish makes a second post, so a
duplicate is visible instead of silently collapsed. It also plays the part of a platform that
holds a scheduled post and publishes it by itself at the slot (native scheduling).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from dk_publishing.adapters.clock import SystemClock
from dk_publishing.application.ports import Clock, Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import Rejected
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation

DELIVERIES = ("direct", "native")


class DryRunLedger(Protocol):
    def record(
        self,
        *,
        tenant_id: str,
        variant_id: str,
        platform: str,
        post: LivePost,
        payload: Handle,
        scheduled_for: datetime | None = None,
    ) -> None: ...

    def posts_for(self, tenant_id: str, variant_id: str) -> list[LivePost]:
        """Every post held or made for the variant, oldest first, excluding cancelled ones.
        More than one means a duplicate."""

    def live_for(self, tenant_id: str, variant_id: str, now: datetime) -> list[LivePost]:
        """The posts that are actually public at `now`: made directly, or whose scheduled time
        has arrived."""

    def cancel(self, tenant_id: str, external_id: str, at: datetime) -> bool:
        """Drop a scheduled post. False if it does not exist (already cancelled or never was)."""


@dataclass
class _Item:
    tenant_id: str
    variant_id: str
    post: LivePost
    scheduled_for: datetime | None
    cancelled: bool = False


@dataclass
class InMemoryLedger:
    items: list[_Item] = field(default_factory=list)

    def record(
        self,
        *,
        tenant_id: str,
        variant_id: str,
        platform: str,
        post: LivePost,
        payload: Handle,
        scheduled_for: datetime | None = None,
    ) -> None:
        self.items.append(_Item(tenant_id, variant_id, post, scheduled_for))

    def posts_for(self, tenant_id: str, variant_id: str) -> list[LivePost]:
        return [
            i.post
            for i in self.items
            if (i.tenant_id, i.variant_id) == (tenant_id, variant_id) and not i.cancelled
        ]

    def live_for(self, tenant_id: str, variant_id: str, now: datetime) -> list[LivePost]:
        return [
            i.post
            for i in self.items
            if (i.tenant_id, i.variant_id) == (tenant_id, variant_id)
            and not i.cancelled
            and (i.scheduled_for is None or i.scheduled_for <= now)
        ]

    def cancel(self, tenant_id: str, external_id: str, at: datetime) -> bool:
        for item in self.items:
            if item.tenant_id == tenant_id and item.post.external_id == external_id:
                if item.cancelled:
                    return False
                item.cancelled = True
                return True
        return False


class PostgresLedger:
    """Durable, and independent of the caller's transaction, like a real platform's own state."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def record(
        self,
        *,
        tenant_id: str,
        variant_id: str,
        platform: str,
        post: LivePost,
        payload: Handle,
        scheduled_for: datetime | None = None,
    ) -> None:
        with psycopg.connect(self._conninfo) as conn:
            conn.execute(
                """INSERT INTO publishing.dry_run_posts
                       (tenant_id, variant_id, platform, external_id, payload, scheduled_for)
                   VALUES (%s, %s::uuid, %s, %s, %s, %s)""",
                (
                    tenant_id,
                    variant_id,
                    platform,
                    post.external_id,
                    Jsonb(dict(payload)),
                    scheduled_for,
                ),
            )

    def _select(
        self, tenant_id: str, variant_id: str, extra: str, params: tuple[Any, ...]
    ) -> list[LivePost]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(
                f"""SELECT external_id, payload->>'url' FROM publishing.dry_run_posts
                    WHERE tenant_id = %s AND variant_id = %s::uuid AND cancelled_at IS NULL {extra}
                    ORDER BY created_at, id""",
                (tenant_id, variant_id, *params),
            ).fetchall()
        return [LivePost(external_id=r[0], url=r[1]) for r in rows]

    def posts_for(self, tenant_id: str, variant_id: str) -> list[LivePost]:
        return self._select(tenant_id, variant_id, "", ())

    def live_for(self, tenant_id: str, variant_id: str, now: datetime) -> list[LivePost]:
        return self._select(
            tenant_id, variant_id, "AND (scheduled_for IS NULL OR scheduled_for <= %s)", (now,)
        )

    def cancel(self, tenant_id: str, external_id: str, at: datetime) -> bool:
        with psycopg.connect(self._conninfo) as conn:
            cursor = conn.execute(
                """UPDATE publishing.dry_run_posts SET cancelled_at = %s
                   WHERE tenant_id = %s AND external_id = %s AND cancelled_at IS NULL""",
                (at, tenant_id, external_id),
            )
        return cursor.rowcount == 1


class DryRunPublisher:
    def __init__(
        self,
        platform: str,
        capabilities: Capabilities,
        ledger: DryRunLedger,
        *,
        max_caption: int = 2200,
        clock: Clock | None = None,
    ) -> None:
        self._platform = platform
        self._capabilities = capabilities
        self._ledger = ledger
        self._max_caption = max_caption
        self._clock = clock or SystemClock()

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        caption = snapshot.content.get("caption")
        problems: list[Violation] = []
        delivery = snapshot.content.get("delivery")
        if delivery and delivery not in DELIVERIES:
            problems.append(
                Violation("delivery", f"delivery must be one of: {', '.join(DELIVERIES)}.")
            )
        if not isinstance(caption, str) or not caption.strip():
            return [*problems, Violation("caption", "Caption is empty.")]
        if len(caption) > self._max_caption:
            problems.append(
                Violation(
                    "caption",
                    f"Caption is {len(caption):,} characters; this platform allows "
                    f"{self._max_caption:,}.",
                )
            )
        return problems

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        return {
            "dry_run": True,
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "content": dict(snapshot.content),
            "media": [r.path for r in media],
        }

    def publish(self, handle: Handle) -> LivePost:
        return self._make(handle)

    def _make(self, handle: Handle, scheduled_for: datetime | None = None) -> LivePost:
        try:
            tenant_id, variant_id = handle["tenant_id"], handle["variant_id"]
        except KeyError:
            raise Rejected("dry-run handle is missing its variant") from None
        external_id = f"dry-{uuid.uuid4().hex[:12]}"
        post = LivePost(external_id, f"https://dry-run.invalid/{self._platform}/{external_id}")
        payload: dict[str, Any] = {"url": post.url, "content": handle.get("content")}
        self._ledger.record(
            tenant_id=tenant_id,
            variant_id=variant_id,
            platform=self._platform,
            post=post,
            payload=payload,
            scheduled_for=scheduled_for,
        )
        return post

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        posts = self._ledger.live_for(snapshot.tenant_id, snapshot.variant_id, self._clock.now())
        return posts[0] if posts else None

    # --- native scheduling: the fake platform holds the post and publishes it by itself ---------

    def schedule(
        self, snapshot: VariantSnapshot, media: Sequence[Rendition], at: datetime
    ) -> Handle:
        handle = self.prepare(snapshot, media)
        post = self._make(handle, scheduled_for=at)
        return {**handle, "scheduled_id": post.external_id, "scheduled_for": at.isoformat()}

    def cancel(self, handle: Handle) -> None:
        scheduled_id = handle.get("scheduled_id")
        if not scheduled_id:
            raise Rejected("dry-run handle has nothing scheduled")
        self._ledger.cancel(str(handle["tenant_id"]), str(scheduled_id), self._clock.now())
