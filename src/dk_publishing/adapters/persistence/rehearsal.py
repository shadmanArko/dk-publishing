"""Development helper: draft variants to rehearse the pipeline with, before real accounts exist."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

import psycopg

from dk_publishing.domain.model import Variant


def create_rehearsal_drafts(
    conninfo: str,
    *,
    tenant_id: str,
    platforms: Sequence[str],
    slots: Sequence[datetime],
    key_prefix: str,
) -> list[Variant]:
    """One post per slot and one draft variant per platform, on rehearsal accounts."""
    variants: list[Variant] = []
    with psycopg.connect(conninfo) as conn:
        accounts: dict[str, str] = {}
        for platform in platforms:
            row = conn.execute(
                """INSERT INTO publishing.social_accounts
                       (tenant_id, platform, external_id, display_name)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (tenant_id, platform, external_id) DO UPDATE SET updated_at = now()
                   RETURNING id::text""",
                (tenant_id, platform, f"rehearsal-{platform}", f"Rehearsal {platform}"),
            ).fetchone()
            assert row is not None
            accounts[platform] = row[0]
        for number, slot in enumerate(slots, start=1):
            post = conn.execute(
                """INSERT INTO publishing.posts (tenant_id, post_key, title)
                   VALUES (%s, %s, %s) RETURNING id::text""",
                (tenant_id, f"{key_prefix}-{number}", f"Rehearsal post {number}"),
            ).fetchone()
            assert post is not None
            for platform in platforms:
                variant = Variant(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    post_id=post[0],
                    platform=platform,
                    account_id=accounts[platform],
                    publish_at=slot,
                )
                conn.execute(
                    """INSERT INTO publishing.variants
                           (id, tenant_id, post_id, platform, account_id, publish_at)
                       VALUES (%s::uuid, %s, %s::uuid, %s, %s::uuid, %s)""",
                    (variant.id, tenant_id, variant.post_id, platform, variant.account_id, slot),
                )
                variants.append(variant)
    return variants
