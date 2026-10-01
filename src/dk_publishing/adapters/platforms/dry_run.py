"""The recording stand-in for any platform: everything real except the final call.

Used to rehearse a full week of Sheet rows before go-live, and for every new adapter. Its "posts"
go to a ledger that behaves like the platform would: a second publish makes a second post, so a
duplicate is visible instead of silently collapsed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from dk_publishing.application.ports import Handle
from dk_publishing.domain.capabilities import Capabilities
from dk_publishing.domain.errors import Rejected
from dk_publishing.domain.publishing import LivePost, Rendition, VariantSnapshot, Violation


class DryRunLedger(Protocol):
    def record(
        self, *, tenant_id: str, variant_id: str, platform: str, post: LivePost, payload: Handle
    ) -> None: ...

    def posts_for(self, tenant_id: str, variant_id: str) -> list[LivePost]:
        """Every post made for the variant, oldest first. More than one means a duplicate."""


@dataclass
class InMemoryLedger:
    _posts: list[tuple[str, str, LivePost]] = field(default_factory=list)

    def record(
        self, *, tenant_id: str, variant_id: str, platform: str, post: LivePost, payload: Handle
    ) -> None:
        self._posts.append((tenant_id, variant_id, post))

    def posts_for(self, tenant_id: str, variant_id: str) -> list[LivePost]:
        return [p for t, v, p in self._posts if (t, v) == (tenant_id, variant_id)]


class PostgresLedger:
    """Durable, and independent of the caller's transaction, like a real platform's own state."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def record(
        self, *, tenant_id: str, variant_id: str, platform: str, post: LivePost, payload: Handle
    ) -> None:
        with psycopg.connect(self._conninfo) as conn:
            conn.execute(
                """INSERT INTO publishing.dry_run_posts
                       (tenant_id, variant_id, platform, external_id, payload)
                   VALUES (%s, %s::uuid, %s, %s, %s)""",
                (tenant_id, variant_id, platform, post.external_id, Jsonb(dict(payload))),
            )

    def posts_for(self, tenant_id: str, variant_id: str) -> list[LivePost]:
        with psycopg.connect(self._conninfo) as conn:
            rows = conn.execute(
                """SELECT external_id, payload->>'url' FROM publishing.dry_run_posts
                   WHERE tenant_id = %s AND variant_id = %s::uuid ORDER BY created_at, id""",
                (tenant_id, variant_id),
            ).fetchall()
        return [LivePost(external_id=r[0], url=r[1]) for r in rows]


class DryRunPublisher:
    def __init__(
        self,
        platform: str,
        capabilities: Capabilities,
        ledger: DryRunLedger,
        *,
        max_caption: int = 2200,
    ) -> None:
        self._platform = platform
        self._capabilities = capabilities
        self._ledger = ledger
        self._max_caption = max_caption

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def validate(self, snapshot: VariantSnapshot) -> list[Violation]:
        caption = snapshot.content.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            return [Violation("caption", "Caption is empty.")]
        if len(caption) > self._max_caption:
            return [
                Violation(
                    "caption",
                    f"Caption is {len(caption):,} characters; this platform allows "
                    f"{self._max_caption:,}.",
                )
            ]
        return []

    def prepare(self, snapshot: VariantSnapshot, media: Sequence[Rendition]) -> Handle:
        return {
            "dry_run": True,
            "tenant_id": snapshot.tenant_id,
            "variant_id": snapshot.variant_id,
            "content": dict(snapshot.content),
            "media": [r.path for r in media],
        }

    def publish(self, handle: Handle) -> LivePost:
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
        )
        return post

    def find_live(self, snapshot: VariantSnapshot, handle: Handle | None) -> LivePost | None:
        posts = self._ledger.posts_for(snapshot.tenant_id, snapshot.variant_id)
        return posts[0] if posts else None
