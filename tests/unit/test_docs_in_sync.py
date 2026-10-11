"""The documents must say what the code does (docs/CHANGE-MAP.md says what to touch when).

Each test fails with the exact thing that drifted, so a change that forgot a guide, the agent text, the Sheet
instructions or the PDFs cannot pass `make check`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from dk_publishing.adapters.media.uploads import DEFAULT_MAX_BYTES
from dk_publishing.adapters.platforms.guides import GUIDES
from dk_publishing.application.use_cases.media_retention import GRACE, STOP_PERCENT
from dk_publishing.composition import DEFAULT_KEY_QUOTA_MB
from dk_publishing.domain.status import VariantStatus

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "dk_publishing"
GUIDE_DIR = ROOT / "docs" / "guides"
SHEET = GUIDE_DIR / "01-post-with-the-sheet.md"
API = GUIDE_DIR / "02-api-for-developers-and-agents.md"
LLMS = SRC / "entrypoints" / "publish_api" / "llms.txt"
SHEET_YAML = yaml.safe_load((ROOT / "config" / "sheet.yaml").read_text())


def text(path: Path) -> str:
    return path.read_text()


def has(document: str, word: str) -> bool:
    return f"`{word}`" in document


# --- the PDFs ---------------------------------------------------------------------------------


def test_the_pdf_guides_were_rebuilt_after_the_last_edit() -> None:
    """Edited a guide? Run `make guides` (needs Chrome) and commit the PDFs and SOURCES.sha256."""
    recorded: dict[str, str] = {}
    for line in text(GUIDE_DIR / "pdf" / "SOURCES.sha256").splitlines():
        digest, name = line.split(maxsplit=1)
        recorded[name] = digest
    for name in ("01-post-with-the-sheet.md", "02-api-for-developers-and-agents.md"):
        now = hashlib.sha256((GUIDE_DIR / name).read_bytes()).hexdigest()
        assert recorded.get(name) == now, (
            f"{name} changed but the PDF was not rebuilt: run `make guides`"
        )
        assert (GUIDE_DIR / "pdf" / name.replace(".md", ".pdf")).stat().st_size > 10_000


# --- guide 02 and llms.txt (the API) ------------------------------------------------------------


def test_the_api_guide_covers_every_platform_format_option_and_caption_limit() -> None:
    guide = text(API)
    for platform, g in GUIDES.items():
        assert has(guide, platform), f"guide 02 does not mention platform {platform}"
        for fmt in g.formats:
            assert has(guide, fmt.name), f"guide 02 does not mention {platform} format {fmt.name}"
            for option in fmt.fields:
                assert has(guide, option.name), f"guide 02 does not mention option {option.name}"
            if fmt.caption and fmt.caption.max_length:
                assert (
                    f"{fmt.caption.max_length:,}" in guide or str(fmt.caption.max_length) in guide
                ), (
                    f"guide 02 lacks the {platform} {fmt.name} caption limit {fmt.caption.max_length}"
                )
            for kind in fmt.media.types:
                assert kind in guide, f"guide 02 lacks file type {kind}"


def codes_in_source() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        body = path.read_text()
        found |= set(re.findall(r'IntakeRejected\(\s*"(\w+)"', body))
    found |= set(
        re.findall(
            r'Problem\(\s*[^,()]+,\s*"([a-z_]+)"', text(SRC / "application/use_cases/api_posts.py")
        )
    )
    found |= set(
        re.findall(
            r'Problem\(\s*[^,()]+,\s*\n?\s*"([a-z_]+)"',
            text(SRC / "application/use_cases/api_posts.py"),
        )
    )
    found |= {
        "unknown_field",
        "media_expired",
    }  # raised through other paths, still part of the contract
    return found


def test_every_error_code_is_explained_in_the_guide_and_the_agent_text() -> None:
    codes = codes_in_source()
    assert {"too_long", "quota_exceeded", "storage_full", "post_key_taken"} <= codes
    for code in sorted(codes):
        assert has(text(API), code), f"guide 02 does not explain error code {code}"
        assert re.search(rf"\b{code}\b", text(LLMS)), f"llms.txt does not explain error code {code}"


def test_every_api_route_is_in_the_guide_and_the_agent_text() -> None:
    app = text(SRC / "entrypoints" / "publish_api" / "app.py")
    routes = re.findall(r'@app\.(get|post|delete)\(\s*"(/v1[^"]*)"', app)
    assert len(routes) >= 8
    for method, path in routes:
        pattern = rf"{method.upper()}\s+{re.escape(path)}"
        if "validate" in path or "approve" in path:
            pattern = re.escape(path)
        assert re.search(pattern, text(LLMS)), f"llms.txt lacks {method.upper()} {path}"
        stem = path.split("{")[0].rstrip("/")
        assert stem in text(API), f"guide 02 lacks {path}"


def test_the_stated_limits_are_the_real_ones() -> None:
    assert f"{DEFAULT_MAX_BYTES // 1024**2} MB" in text(
        API
    ) and f"{DEFAULT_MAX_BYTES // 1024**2} MB" in text(LLMS)
    assert f"{DEFAULT_KEY_QUOTA_MB // 1024} GB" in text(
        API
    ) and f"{DEFAULT_KEY_QUOTA_MB // 1024} GB" in text(LLMS)
    assert f"{STOP_PERCENT}%" in text(API) or "nearly full" in text(API)
    assert GRACE.days == 7 and "a week" in text(API) and "a week" in text(LLMS)


# --- guide 01 and the Sheet ---------------------------------------------------------------------


def test_the_sheet_guide_names_every_required_column_and_every_calendar_column() -> None:
    guide = text(SHEET)
    for platform, columns in SHEET_YAML["platform_extras"].items():
        for column in columns:
            if column.get("required"):
                assert has(guide, column["name"]) or column["name"] in guide, (
                    f"guide 01 never mentions the required {platform} column {column['name']}"
                )
    for column in SHEET_YAML["calendar"]:
        assert column["name"] in guide, (
            f"guide 01 does not explain Calendar column {column['name']}"
        )
    for column in SHEET_YAML["posts"]:
        assert column["name"] in guide, f"guide 01 does not explain Posts column {column['name']}"


def test_the_sheet_guide_lists_every_format_the_dropdowns_offer() -> None:
    guide = text(SHEET)
    for platform, columns in SHEET_YAML["platform_extras"].items():
        for column in columns:
            if column["name"] == "format":
                for option in column["options"]:
                    assert has(guide, option), f"guide 01 lacks {platform} format {option}"


def test_the_sheet_guide_explains_every_status_the_sheet_can_show() -> None:
    guide = text(SHEET)
    for status in VariantStatus:
        if status is VariantStatus.PENDING_APPROVAL:
            continue  # reserved: the Sheet never produces it
        assert has(guide, status.value), f"guide 01 lacks the status {status.value}"


def test_the_instructions_inside_the_sheet_name_the_same_things_as_the_guide() -> None:
    readme = "\n".join(SHEET_YAML["readme"])
    for word in ("post_key", "ready", "enabled", "slot", "Drive"):
        assert word in readme and word in text(SHEET), word


# --- the maps ----------------------------------------------------------------------------------


def test_every_adr_is_in_the_index() -> None:
    index = text(ROOT / "docs" / "adr" / "README.md")
    for adr in sorted((ROOT / "docs" / "adr").glob("0*.md")):
        assert adr.name in index, f"{adr.name} is missing from docs/adr/README.md"


def test_migrations_are_numbered_without_gaps_or_repeats() -> None:
    numbers = [int(p.name[:4]) for p in sorted((ROOT / "migrations").glob("0*.sql"))]
    assert numbers == list(range(1, len(numbers) + 1))


def test_the_change_map_exists_is_read_first_and_points_only_at_real_files() -> None:
    change_map = ROOT / "docs" / "CHANGE-MAP.md"
    assert "docs/CHANGE-MAP.md" in text(ROOT / "CLAUDE.md")
    for path in re.findall(
        r"`((?:src|tests|docs|config|deploy|migrations|scripts)/[\w./-]+)`", text(change_map)
    ):
        assert (ROOT / path).exists(), f"CHANGE-MAP.md points at {path}, which does not exist"


@pytest.mark.parametrize("guide", sorted(GUIDE_DIR.glob("*.md")), ids=lambda p: p.name)
def test_every_relative_link_in_the_guides_resolves(guide: Path) -> None:
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text(guide)):
        if not target.startswith(("http://", "https://", "mailto:")):
            assert (guide.parent / target).exists(), f"{guide.name} links to missing {target}"
