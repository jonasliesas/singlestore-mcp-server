"""Render the SAS SpeedyStore Studio mark (the splash's logo) into scripts/studio.ico for the desktop shortcut.

    .venv\\Scripts\\python.exe scripts/make_studio_icon.py

A developer step (needs Playwright + Edge, the test dependencies): the .ico is committed, so the installer only
copies it. Run it again after the mark in apps/splash.html changes.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from singlestore_mcp.apps import splash  # noqa: E402

OUT = ROOT / "scripts" / "studio.ico"
SIZES = (16, 24, 32, 48, 64, 256)


def main() -> None:
    svg = splash._static_mark()
    pngs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page()
        for size in SIZES:
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(
                "<html><body style='margin:0;background:transparent'>"
                f"<div style='width:{size}px;height:{size}px'>{svg}</div></body></html>")
            page.eval_on_selector("svg", "s => { s.setAttribute('width', '100%'); s.setAttribute('height', '100%'); }")
            pngs.append(page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size}))
        browser.close()
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, img in zip(SIZES, pngs):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(img), offset + len(data))
        data += img
    OUT.write_bytes(header + entries + data)
    print("Wrote", OUT, f"({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
