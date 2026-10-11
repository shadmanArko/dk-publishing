"""Build the PDF versions of the guides in docs/guides/ (`make guides`).

Markdown -> HTML (markdown-it-py, which dagster already installs) -> PDF with headless Chrome.
Also writes docs/guides/pdf/SOURCES.sha256, the fingerprint of each Markdown file the PDFs
were built from; tests/unit/test_docs_in_sync.py fails when a guide changed without a rebuild.
The style is in scripts/guides.css.

Chrome is only needed here, never in CI: set CHROME to override the default macOS path.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / "docs" / "guides"
GUIDE_FILES = {
    "01-post-with-the-sheet": "Guide 1: Post with the Google Sheet and Drive",
    "02-api-for-developers-and-agents": "Guide 2: The Publishing API",
}
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
MANIFEST = GUIDES / "pdf" / "SOURCES.sha256"

CSS = (Path(__file__).with_name("guides.css")).read_text()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(markdown: str, footer: str) -> str:
    md = MarkdownIt("commonmark").enable("table")
    default_fence = md.renderer.rules["fence"]

    def fence(tokens, idx, options, env):  # type: ignore[no-untyped-def]
        out = default_fence(tokens, idx, options, env)
        if any(ch in tokens[idx].content for ch in "─│┌└►"):  # a box diagram: never wrap it
            out = out.replace("<pre>", "<pre class='diagram'>", 1)
        return out

    md.renderer.rules["fence"] = fence
    cover = f"<div class='cover'>Dhaka Kacchi · DK Publishing · {footer}</div>"
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{footer}</title>"
        f"<style>{CSS}</style></head><body>{cover}{md.render(markdown)}</body></html>"
    )


def main() -> int:
    if not Path(CHROME).exists():
        print(f"Chrome not found at {CHROME}. Set CHROME=/path/to/chrome.", file=sys.stderr)
        return 1
    (GUIDES / "pdf").mkdir(exist_ok=True)
    lines = []
    with tempfile.TemporaryDirectory() as work:
        for stem, title in GUIDE_FILES.items():
            source = GUIDES / f"{stem}.md"
            page = Path(work) / f"{stem}.html"
            page.write_text(render(source.read_text(), title))
            pdf = GUIDES / "pdf" / f"{stem}.pdf"
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                 f"--print-to-pdf={pdf}", f"file://{page}"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
            )  # fmt: skip
            lines.append(f"{digest(source)}  {stem}.md")
            print(f"built {pdf.relative_to(ROOT)}")
    MANIFEST.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
