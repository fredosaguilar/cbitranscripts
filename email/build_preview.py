#!/usr/bin/env python3
"""Build a self-contained preview of the Columbia Basin Insurance email.

Takes columbia-basin-interactive-email.html, swaps the {{ASSET_BASE}} image
URLs for inlined base64 data URIs and fills the merge tags with sample
values, then writes preview.html. Open that file in a browser to click
through the tabs and the FAQ exactly the way Apple Mail renders them.

    python3 email/build_preview.py
"""

import base64
import mimetypes
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "email" / "columbia-basin-interactive-email.html"
ASSETS = ROOT / "static" / "email"
OUTPUT = ROOT / "email" / "preview.html"

SAMPLE_MERGE_VALUES = {
    "{{FIRST_NAME}}": "Sam",
    "{{VIEW_IN_BROWSER_URL}}": "#",
    "{{UNSUBSCRIBE_URL}}": "#",
    "{{PREFERENCES_URL}}": "#",
}


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def main() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")

    for asset in sorted(p for p in ASSETS.iterdir() if p.suffix in {".png", ".jpg", ".gif"}):
        html = html.replace("{{ASSET_BASE}}/" + asset.name, data_uri(asset))

    leftover = re.findall(r"\{\{ASSET_BASE\}\}/[\w.-]+", html)
    if leftover:
        raise SystemExit(f"missing asset files for: {', '.join(sorted(set(leftover)))}")

    for tag, value in SAMPLE_MERGE_VALUES.items():
        html = html.replace(tag, value)

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
