from hypothesis import given
from hypothesis import strategies as st

from dk_publishing.domain.snapshot import canonical_json, snapshot_hash


def test_key_order_does_not_change_the_hash() -> None:
    assert snapshot_hash({"a": 1, "b": [1, 2]}) == snapshot_hash({"b": [1, 2], "a": 1})


def test_any_content_change_changes_the_hash() -> None:
    assert snapshot_hash({"caption": "Eid platter"}) != snapshot_hash({"caption": "Eid platter!"})


def test_non_ascii_text_is_kept_as_written() -> None:
    assert canonical_json({"c": "ঢাকা কাচ্চি"}) == '{"c":"ঢাকা কাচ্চি"}'


@given(st.dictionaries(st.text(), st.one_of(st.integers(), st.text(), st.booleans(), st.none())))
def test_hashing_is_deterministic(content: dict[str, object]) -> None:
    assert snapshot_hash(content) == snapshot_hash(dict(reversed(list(content.items()))))
