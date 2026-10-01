from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest

from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.domain.lifecycle import transition
from dk_publishing.domain.model import Actor, ActorKind, Variant
from dk_publishing.domain.planning import Action, NextStep
from dk_publishing.domain.snapshot import snapshot_hash
from dk_publishing.domain.status import VariantStatus
from tests.integration.conftest import NOW, TENANT, Seed

S = VariantStatus
ME = Actor(ActorKind.HUMAN, "shadman")
CONTENT = {"caption": "Eid platter", "media": ["a.mp4"]}
HASH = snapshot_hash(CONTENT)


def new_variant(seed: Seed, key: str = "DK-1") -> Variant:
    post_id, account_id = seed.post_and_account(key)
    return Variant(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        post_id=post_id,
        platform="p",
        account_id=account_id,
        publish_at=NOW + timedelta(hours=3),
    )


def step(v: Variant, to: S, **kw: str | None) -> tuple[Variant, object]:
    return transition(v, to, actor=ME, reason="test", at=NOW, **kw)


def persist_approved(conninfo: str, seed: Seed, to: S = S.APPROVED) -> Variant:
    """A variant that has been approved (and optionally moved further), committed to the DB."""
    draft = new_variant(seed)
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(draft)
        approved, event = transition(
            draft, S.APPROVED, actor=ME, reason="ready", at=NOW, snapshot_hash=HASH
        )
        assert uow.variants.apply(draft, approved, event, None, snapshot=CONTENT)
        current = approved
        if to is S.PREPARED:
            for nxt in (S.PREPARING, S.PREPARED):
                moved, event = transition(current, nxt, actor=ME, reason="prep", at=NOW)
                assert uow.variants.apply(current, moved, event, None)
                current = moved
        uow.commit()
    return current


def test_add_and_get_round_trip(conninfo: str, seed: Seed) -> None:
    variant = new_variant(seed)
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(variant)
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.get(variant.id) == variant
        assert uow.variants.get(str(uuid.uuid4())) is None


def test_only_fresh_drafts_can_be_added(conninfo: str, seed: Seed) -> None:
    advanced = replace(new_variant(seed), version=1)
    with PostgresUnitOfWork(conninfo) as uow, pytest.raises(ValueError, match="fresh draft"):
        uow.variants.add(advanced)


def test_apply_writes_state_event_and_next_action_together(conninfo: str, seed: Seed) -> None:
    draft = new_variant(seed)
    due = NextStep(Action.PREPARE, NOW + timedelta(hours=2, minutes=30))
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(draft)
        approved, event = transition(
            draft, S.APPROVED, actor=ME, reason="ready", at=NOW, snapshot_hash=HASH
        )
        assert uow.variants.apply(draft, approved, event, due, snapshot=CONTENT)
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow:
        stored = uow.variants.get(draft.id)
        assert stored == approved
        assert uow.variants.snapshot_of(draft.id) == CONTENT
        assert [e.to_status for e in uow.variants.events(draft.id)] == [S.APPROVED]
        assert uow.variants.due(NOW, 10) == []
        [item] = uow.variants.due(NOW + timedelta(hours=3), 10)
        assert (item.variant_id, item.action, item.version) == (draft.id, Action.PREPARE, 1)


def test_due_is_ordered_and_limited(conninfo: str, seed: Seed) -> None:
    ids = []
    with PostgresUnitOfWork(conninfo) as uow:
        for i, minutes in enumerate((30, 10, 20)):
            draft = new_variant(seed, f"DK-{i}")
            uow.variants.add(draft)
            approved, event = transition(
                draft, S.APPROVED, actor=ME, reason="r", at=NOW, snapshot_hash=HASH
            )
            at = NOW + timedelta(minutes=minutes)
            uow.variants.apply(
                draft, approved, event, NextStep(Action.PREPARE, at), snapshot=CONTENT
            )
            ids.append((minutes, draft.id))
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow:
        got = [d.variant_id for d in uow.variants.due(NOW + timedelta(hours=1), 2)]
    assert got == [i for _, i in sorted(ids)][:2]


def test_a_stale_writer_loses_and_leaves_no_event(conninfo: str, seed: Seed) -> None:
    approved = persist_approved(conninfo, seed)
    moved, event = transition(approved, S.PREPARING, actor=ME, reason="a", at=NOW)
    other, other_event = transition(approved, S.CANCELLED, actor=ME, reason="b", at=NOW)
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.apply(approved, moved, event, None)
        assert not uow.variants.apply(approved, other, other_event, None)  # stale: lost
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.get(approved.id) == moved
        assert [e.to_status for e in uow.variants.events(approved.id)] == [S.APPROVED, S.PREPARING]


def test_exactly_one_of_many_racing_runs_enters_publishing(conninfo: str, seed: Seed) -> None:
    """The duplicate guarantee: compare-and-set on (status, version) admits a single publisher."""
    prepared = persist_approved(conninfo, seed, to=S.PREPARED)
    racers = 8
    barrier = threading.Barrier(racers)
    wins: list[bool] = []
    errors: list[BaseException] = []

    def race() -> None:
        try:
            publishing, event = transition(prepared, S.PUBLISHING, actor=ME, reason="go", at=NOW)
            with PostgresUnitOfWork(conninfo) as uow:
                barrier.wait()
                won = uow.variants.apply(prepared, publishing, event, None)
                uow.commit()
            wins.append(won)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=race) for _ in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert sorted(wins) == [False] * (racers - 1) + [True]
    with PostgresUnitOfWork(conninfo) as uow:
        events = uow.variants.events(prepared.id)
    assert [e.to_status for e in events].count(S.PUBLISHING) == 1


def test_inconsistent_arguments_are_rejected_before_touching_the_database(
    conninfo: str, seed: Seed
) -> None:
    approved = persist_approved(conninfo, seed)
    moved, event = transition(approved, S.PREPARING, actor=ME, reason="a", at=NOW)
    skipped_version = replace(moved, version=5)
    with PostgresUnitOfWork(conninfo) as uow:
        with pytest.raises(ValueError, match="advance by exactly one"):
            uow.variants.apply(approved, skipped_version, event, None)
        with pytest.raises(ValueError, match="only accepted on the transition that freezes"):
            uow.variants.apply(approved, moved, event, None, snapshot=CONTENT)


def test_freezing_requires_matching_content(conninfo: str, seed: Seed) -> None:
    draft = new_variant(seed)
    approved, event = transition(
        draft, S.APPROVED, actor=ME, reason="r", at=NOW, snapshot_hash=HASH
    )
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(draft)
        with pytest.raises(ValueError, match="requires its content"):
            uow.variants.apply(draft, approved, event, None)
        with pytest.raises(ValueError, match="does not match its hash"):
            uow.variants.apply(draft, approved, event, None, snapshot={"caption": "tampered"})


def test_editing_after_approval_clears_the_snapshot(conninfo: str, seed: Seed) -> None:
    approved = persist_approved(conninfo, seed)
    drafted, event = transition(approved, S.DRAFT, actor=ME, reason="edited", at=NOW)
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.apply(approved, drafted, event, None)
        uow.commit()
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.snapshot_of(approved.id) is None
        stored = uow.variants.get(approved.id)
        assert stored is not None and stored.snapshot_hash is None


def test_publishing_stamps_published_at_from_the_event(conninfo: str, seed: Seed) -> None:
    prepared = persist_approved(conninfo, seed, to=S.PREPARED)
    live_at = NOW + timedelta(hours=3, seconds=41)
    publishing, e1 = transition(prepared, S.PUBLISHING, actor=ME, reason="go", at=live_at)
    published, e2 = transition(publishing, S.PUBLISHED, actor=ME, reason="ok", at=live_at)
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.apply(prepared, publishing, e1, None)
        assert uow.variants.apply(publishing, published, e2, None)
        uow.commit()
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "SELECT published_at, next_action FROM publishing.variants WHERE id = %s::uuid",
            (prepared.id,),
        ).fetchone()
    assert row == (live_at, None)


def test_uncommitted_work_is_rolled_back(conninfo: str, seed: Seed) -> None:
    variant = new_variant(seed)
    with pytest.raises(RuntimeError), PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(variant)
        raise RuntimeError("boom")
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.get(variant.id) is None
    with PostgresUnitOfWork(conninfo) as uow:  # leaving the block without commit() also rolls back
        uow.variants.add(variant)
    with PostgresUnitOfWork(conninfo) as uow:
        assert uow.variants.get(variant.id) is None


# The database refuses what the domain would never produce --------------------------------------


def _raw(conninfo: str, sql: str, *params: object) -> None:
    with psycopg.connect(conninfo) as conn:
        conn.execute(sql, params)


def test_events_are_append_only(conninfo: str, seed: Seed) -> None:
    approved = persist_approved(conninfo, seed)
    with pytest.raises(psycopg.errors.RestrictViolation):
        _raw(conninfo, "UPDATE publishing.variant_events SET reason = 'x'")
    with pytest.raises(psycopg.errors.RestrictViolation):
        _raw(
            conninfo,
            "DELETE FROM publishing.variant_events WHERE variant_id = %s::uuid",
            approved.id,
        )


def test_the_database_refuses_an_approved_variant_without_a_snapshot(
    conninfo: str, seed: Seed
) -> None:
    draft = new_variant(seed)
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(draft)
        uow.commit()
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_variants_snapshot_when_approved"):
        _raw(
            conninfo,
            "UPDATE publishing.variants SET status = 'approved' WHERE id = %s::uuid",
            draft.id,
        )


def test_the_database_refuses_publishing_a_never_approved_variant(
    conninfo: str, seed: Seed
) -> None:
    draft = new_variant(seed)
    with PostgresUnitOfWork(conninfo) as uow:
        uow.variants.add(draft)
        uow.commit()
    with pytest.raises(psycopg.errors.CheckViolation, match="never approved"):
        _raw(
            conninfo,
            """INSERT INTO publishing.variant_events
                   (tenant_id, variant_id, seq, from_status, to_status, actor_kind, actor_name, reason, at)
               VALUES (%s, %s::uuid, 1, 'draft', 'published', 'system', 's', 'forged', now())""",
            TENANT,
            draft.id,
        )


def test_a_variant_cannot_point_at_another_tenants_post(conninfo: str, seed: Seed) -> None:
    variant = new_variant(seed)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _raw(
            conninfo,
            """INSERT INTO publishing.variants (tenant_id, post_id, platform, account_id, publish_at)
               VALUES ('someone-else', %s::uuid, 'p', %s::uuid, now())""",
            variant.post_id,
            variant.account_id,
        )
