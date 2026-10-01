from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dk_publishing.domain.lifecycle import IllegalTransition, transition
from dk_publishing.domain.model import Actor, ActorKind, Variant, VariantEvent
from dk_publishing.domain.status import ALLOWED, VariantStatus

S = VariantStatus
ME = Actor(ActorKind.HUMAN, "shadman")
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def make(status: S = S.DRAFT, snapshot: str | None = None, version: int = 0) -> Variant:
    return Variant(
        id="v1",
        tenant_id="t1",
        post_id="p1",
        platform="p",
        account_id="a1",
        publish_at=T0,
        status=status,
        version=version,
        snapshot_hash=snapshot,
    )


def go(v: Variant, to: S, **kw: str | None) -> tuple[Variant, VariantEvent]:
    return transition(v, to, actor=ME, reason="test", at=T0, **kw)


def test_approving_freezes_the_snapshot_and_bumps_the_version() -> None:
    moved, event = go(make(), S.APPROVED, snapshot_hash="abc")
    assert (moved.status, moved.snapshot_hash, moved.version) == (S.APPROVED, "abc", 1)
    assert (event.seq, event.from_status, event.to_status) == (1, S.DRAFT, S.APPROVED)


def test_approving_without_a_hash_is_refused() -> None:
    with pytest.raises(IllegalTransition, match="snapshot hash"):
        go(make(), S.APPROVED)


def test_a_hash_cannot_be_supplied_on_other_moves() -> None:
    with pytest.raises(IllegalTransition, match="cannot be supplied"):
        go(make(S.APPROVED, "abc"), S.PREPARING, snapshot_hash="other")


def test_retrying_back_to_approved_keeps_the_original_snapshot() -> None:
    moved, _ = go(make(S.PREPARING, "abc", 3), S.APPROVED)
    assert moved.snapshot_hash == "abc"


def test_an_edit_returns_to_draft_and_clears_the_snapshot() -> None:
    moved, _ = go(make(S.PREPARED, "abc", 4), S.DRAFT)
    assert (moved.status, moved.snapshot_hash) == (S.DRAFT, None)


def test_drafting_cannot_carry_a_hash() -> None:
    with pytest.raises(IllegalTransition):
        go(make(S.PREPARED, "abc"), S.DRAFT, snapshot_hash="abc")


def test_illegal_moves_are_refused() -> None:
    with pytest.raises(IllegalTransition, match="not allowed"):
        go(make(S.DRAFT), S.PUBLISHED)


def test_a_reason_is_mandatory() -> None:
    with pytest.raises(IllegalTransition, match="reason"):
        transition(make(), S.INVALID, actor=ME, reason="  ", at=T0)


def test_an_approved_state_without_a_snapshot_is_refused() -> None:
    corrupt = make(S.PREPARING, snapshot=None)
    with pytest.raises(IllegalTransition, match="requires an approved snapshot"):
        go(corrupt, S.PREPARED)


def test_event_time_is_normalised_to_utc() -> None:
    from datetime import timedelta, timezone

    berlin = timezone(timedelta(hours=2))
    _, event = transition(
        make(), S.INVALID, actor=ME, reason="bad", at=datetime(2026, 11, 14, 14, 0, tzinfo=berlin)
    )
    assert event.at == datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    assert event.at.utcoffset() == timedelta(0)


@st.composite
def walks(draw: st.DrawFn) -> list[tuple[Variant, VariantEvent]]:
    variant = make()
    steps: list[tuple[Variant, VariantEvent]] = []
    for _ in range(draw(st.integers(0, 40))):
        options = sorted(ALLOWED[variant.status])
        if not options:
            break
        to = draw(st.sampled_from(options))
        needs_hash = to is S.APPROVED and variant.snapshot_hash is None
        variant, event = go(variant, to, snapshot_hash="h" if needs_hash else None)
        steps.append((variant, event))
    return steps


@given(walks())
def test_no_sequence_of_legal_moves_publishes_without_approval(
    steps: list[tuple[Variant, VariantEvent]],
) -> None:
    seen = [event.to_status for _, event in steps]
    if S.PUBLISHED in seen:
        assert S.APPROVED in seen[: seen.index(S.PUBLISHED)]


@given(walks())
def test_events_form_an_unbroken_chain_and_versions_count_up(
    steps: list[tuple[Variant, VariantEvent]],
) -> None:
    previous = S.DRAFT
    for index, (variant, event) in enumerate(steps, start=1):
        assert event.from_status is previous
        assert event.seq == variant.version == index
        previous = event.to_status
        assert variant.status is event.to_status


@given(walks())
def test_an_approved_state_always_carries_its_snapshot(
    steps: list[tuple[Variant, VariantEvent]],
) -> None:
    from dk_publishing.domain.status import REQUIRES_SNAPSHOT

    for variant, _ in steps:
        assert (variant.snapshot_hash is not None) or variant.status not in REQUIRES_SNAPSHOT
