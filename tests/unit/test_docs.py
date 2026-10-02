"""The setup guides are the product for a new business: no broken links, every section covered."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dk_publishing.adapters.platforms.dk_file import TEMPLATE

ROOT = Path(__file__).resolve().parents[2]
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs" / "setup").glob("*.md"))]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_relative_link_resolves(page: Path) -> None:
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", page.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert (page.parent / target).exists(), f"{page.name} links to missing {target}"


def test_the_readme_links_a_guide_for_every_section_of_dk_json() -> None:
    readme = (ROOT / "README.md").read_text()
    for guide in ("google", "meta", "youtube", "telegram"):
        assert f"docs/setup/{guide}.md" in readme and guide in TEMPLATE
    assert (ROOT / ".env.example").read_text().count(
        "="
    ) == 2  # only DATABASE_URL and DK_CONFIG_FILE


def test_every_guide_named_in_the_template_exists() -> None:
    for section in TEMPLATE.values():
        if isinstance(section, dict):
            for match in re.findall(r"docs/setup/(\w[\w-]*\.md)", str(section.get("_help", ""))):
                assert (ROOT / "docs" / "setup" / match).exists()
