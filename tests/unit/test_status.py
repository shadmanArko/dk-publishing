from collections import deque

from dk_publishing.domain.status import ALLOWED, FINAL, IN_FLIGHT, REQUIRES_SNAPSHOT, VariantStatus

S = VariantStatus


def _reachable(start: S, *, avoiding: S | None = None) -> set[S]:
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in ALLOWED[queue.popleft()]:
            if nxt not in seen and nxt is not avoiding:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def test_every_status_has_a_row() -> None:
    assert set(ALLOWED) == set(S)


def test_every_status_is_reachable_from_draft() -> None:
    assert _reachable(S.DRAFT) == set(S)


def test_published_is_never_reachable_without_passing_approved() -> None:
    """Invariant 1, proved on the graph itself rather than sampled."""
    assert S.PUBLISHED not in _reachable(S.DRAFT, avoiding=S.APPROVED)


def test_nothing_after_approval_is_reachable_without_approval() -> None:
    skipped = _reachable(S.DRAFT, avoiding=S.APPROVED)
    assert not skipped & REQUIRES_SNAPSHOT


def test_published_is_terminal() -> None:
    assert ALLOWED[S.PUBLISHED] == frozenset()


def test_final_states_only_lead_back_to_draft() -> None:
    for status in FINAL - {S.PUBLISHED}:
        assert ALLOWED[status] == {S.DRAFT}


def test_in_flight_states_cannot_be_edited_or_cancelled_mid_run() -> None:
    for status in IN_FLIGHT:
        assert not ALLOWED[status] & {S.DRAFT, S.CANCELLED}


def test_unknown_never_goes_straight_to_a_retry_state_except_prepared_after_reconcile() -> None:
    assert ALLOWED[S.UNKNOWN] == {S.PUBLISHED, S.PREPARED, S.FAILED}
