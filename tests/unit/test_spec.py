"""The publishing contract's JSON Schemas must agree with what the adapters really enforce.

A client checks a post offline with the schema; the API's check stays the authority. So the schema
may never refuse an example the API accepts, and where both look at the same rule (caption length,
how many files, a choice, a required option) they must give the same answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from tests.support import T0
from tests.support.api_rig import publishers

from dk_publishing.adapters.platforms.guides import GUIDES
from dk_publishing.application.use_cases.api_posts import request_example
from dk_publishing.domain.guide import FormatGuide
from dk_publishing.domain.publishing import VariantSnapshot
from dk_publishing.domain.spec import variant_schema

CASES = [(g.platform, f) for g in GUIDES.values() for f in g.formats]
IDS = [f"{p}-{f.name}" for p, f in CASES]
ACCOUNTS = ["Dhaka Kacchi"]
WORDS = st.text(alphabet="abcdefghijklmnop qrstuvwxyz", min_size=0, max_size=1)


def schema_ok(platform: str, fmt: FormatGuide, variant: dict[str, Any]) -> bool:
    return Draft202012Validator(variant_schema(platform, fmt, ACCOUNTS)).is_valid(variant)


def adapter_problems(tmp_path: Path, platform: str, fmt: FormatGuide, **more: Any) -> list[Any]:
    content = {**fmt.example, **more}
    count = len(content.get("media", []))
    kind = fmt.media.types[0] if fmt.media.types else ".jpg"  # a text format names no type
    content["media"] = [{"name": f"f{i}{kind}"} for i in range(count)]
    snapshot = VariantSnapshot("v", "dk", platform, "a", T0, content)
    return publishers(tmp_path)[platform].validate(snapshot)


@pytest.mark.parametrize(("platform", "fmt"), CASES, ids=IDS)
def test_the_schema_accepts_every_example_the_api_hands_out(
    platform: str, fmt: FormatGuide
) -> None:
    assert schema_ok(platform, fmt, request_example(platform, fmt.example))


@pytest.mark.parametrize(("platform", "fmt"), CASES, ids=IDS)
def test_the_schema_refuses_what_the_rules_refuse(platform: str, fmt: FormatGuide) -> None:
    good = request_example(platform, fmt.example)
    assert not schema_ok(platform, fmt, {**good, "surprise": 1})  # unknown fields are refused
    assert not schema_ok(platform, fmt, {**good, "options": {"surprise": 1}})
    assert not schema_ok(platform, fmt, {**good, "media": ["m"] * (fmt.media.max + 1)})
    if fmt.media.min > 0:
        assert not schema_ok(platform, fmt, {k: v for k, v in good.items() if k != "media"})
    if fmt.caption is None:
        assert not schema_ok(platform, fmt, {**good, "caption": "not shown"})
    elif fmt.caption.required:  # found by the property test below: only spaces is no text
        assert not schema_ok(platform, fmt, {**good, "caption": "   "})
    elif fmt.caption.max_length is not None:
        assert not schema_ok(platform, fmt, {**good, "caption": "x" * (fmt.caption.max_length + 1)})
    for option in fmt.fields:
        if option.choices:
            bad = {**good, "options": {**good.get("options", {}), option.name: "no-such-choice"}}
            assert not schema_ok(platform, fmt, bad)
        if option.required:
            rest = {k: v for k, v in good.get("options", {}).items() if k != option.name}
            assert not schema_ok(platform, fmt, {**good, "options": rest})


CAPTIONED = [(p, f) for p, f in CASES if f.caption is not None and f.caption.max_length]


@pytest.mark.parametrize(
    ("platform", "fmt"), CAPTIONED, ids=[f"{p}-{f.name}" for p, f in CAPTIONED]
)
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(extra=st.integers(min_value=-40, max_value=40), word=WORDS)
def test_schema_and_adapter_agree_on_caption_length(
    tmp_path: Path, platform: str, fmt: FormatGuide, extra: int, word: str
) -> None:
    assert fmt.caption is not None and fmt.caption.max_length is not None
    length = max(0, fmt.caption.max_length + extra)
    caption = (word or "a") * length
    if fmt.caption.required and not caption:
        return
    variant = {**request_example(platform, fmt.example), "caption": caption}
    rejected = [p for p in adapter_problems(tmp_path, platform, fmt, caption=caption)
                if p.field == "caption"]  # fmt: skip
    assert schema_ok(platform, fmt, variant) == (not rejected)


@pytest.mark.parametrize(("platform", "fmt"), CASES, ids=IDS)
@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(count=st.integers(min_value=0, max_value=12))
def test_schema_and_adapter_agree_on_how_many_files(
    tmp_path: Path, platform: str, fmt: FormatGuide, count: int
) -> None:
    variant = {**request_example(platform, fmt.example), "media": ["m"] * count}
    if count == 0:
        variant.pop("media")
    rejected = [p for p in adapter_problems(tmp_path, platform, fmt, media=["m"] * count)
                if p.field == "media"]  # fmt: skip
    assert schema_ok(platform, fmt, variant) == (not rejected)
