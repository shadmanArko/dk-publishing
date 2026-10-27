"""The platform guides served by the API must agree with what the adapters really enforce."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.support import T0
from tests.support.api_rig import MEDIA_PLACEHOLDER, publishers

from dk_publishing.adapters.platforms.guides import GUIDES
from dk_publishing.domain.guide import FormatGuide
from dk_publishing.domain.publishing import VariantSnapshot

CASES = [(g.platform, f.name) for g in GUIDES.values() for f in g.formats]


def content(fmt: FormatGuide, **more: Any) -> dict[str, Any]:
    data = dict(fmt.example)
    names = [f"file{i}{fmt.media.types[0]}" for i, _ in enumerate(data.get("media", []))]
    data["media"] = [{"name": n} for n in names]
    return {**data, **more}


def problems(tmp_path: Path, platform: str, data: dict[str, Any]) -> list[Any]:
    snapshot = VariantSnapshot("v", "dk", platform, "a", T0, data)
    return publishers(tmp_path)[platform].validate(snapshot)


def test_every_platform_has_a_guide() -> None:
    assert set(GUIDES) == {"instagram", "facebook", "threads", "youtube", "tiktok", "linkedin"}
    assert MEDIA_PLACEHOLDER == "MEDIA_ID"


@pytest.mark.parametrize(("platform", "fmt"), CASES)
def test_the_example_passes_the_real_validation(tmp_path: Path, platform: str, fmt: str) -> None:
    guide = GUIDES[platform].format(fmt)
    assert guide is not None
    assert problems(tmp_path, platform, content(guide)) == []


@pytest.mark.parametrize(("platform", "fmt"), CASES)
def test_the_stated_text_limit_is_the_real_one(tmp_path: Path, platform: str, fmt: str) -> None:
    guide = GUIDES[platform].format(fmt)
    assert guide is not None
    text_fields = [f for f in (guide.caption, *guide.fields) if f and f.max_length]
    for field in text_fields:
        limit = field.max_length
        assert limit is not None
        ok = content(guide, **{field.name: "x" * limit})
        too_long = content(guide, **{field.name: "x" * (limit + 1)})
        assert not [p for p in problems(tmp_path, platform, ok) if p.field == field.name]
        assert [p for p in problems(tmp_path, platform, too_long) if p.field == field.name]


@pytest.mark.parametrize(("platform", "fmt"), CASES)
def test_every_listed_choice_is_accepted_and_others_are_not(
    tmp_path: Path, platform: str, fmt: str
) -> None:
    guide = GUIDES[platform].format(fmt)
    assert guide is not None
    for field in (f for f in guide.fields if f.choices):
        for choice in field.choices:
            bad = [p for p in problems(tmp_path, platform, content(guide, **{field.name: choice}))]
            assert not [p for p in bad if p.field == field.name], (field.name, choice)
        if field.name == "delivery":
            continue  # checked by the use case (delivery_problems), not by the adapter
        wrong = problems(tmp_path, platform, content(guide, **{field.name: "nonsense"}))
        assert [p for p in wrong if p.field == field.name], field.name


@pytest.mark.parametrize(("platform", "fmt"), CASES)
def test_the_stated_media_count_is_the_real_one(tmp_path: Path, platform: str, fmt: str) -> None:
    guide = GUIDES[platform].format(fmt)
    assert guide is not None
    ext = guide.media.types[0] if guide.media.types else ".mp4"

    def media(n: int) -> dict[str, Any]:
        return content(guide, media=[{"name": f"f{i}{ext}"} for i in range(n)])

    if guide.media.max == 0:
        return
    assert not [
        p for p in problems(tmp_path, platform, media(guide.media.min)) if p.field == "media"
    ]
    assert not [
        p for p in problems(tmp_path, platform, media(guide.media.max)) if p.field == "media"
    ]
    assert [
        p for p in problems(tmp_path, platform, media(guide.media.max + 1)) if p.field == "media"
    ]
    if guide.media.min > 1:
        assert [
            p
            for p in problems(tmp_path, platform, media(guide.media.min - 1))
            if p.field == "media"
        ]
