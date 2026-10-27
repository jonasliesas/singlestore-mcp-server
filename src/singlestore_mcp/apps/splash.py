"""The SAS SpeedyStore Studio splash (logo, name, status line) and the mark as favicon.

One fragment (``splash.html``) for every place the Studio starts: the Workspace page, the browser view's host page
and the desktop shortcut's ``starting.html``. Pages carry ``<!--S2:SPLASH-->`` (body) and ``<!--S2:FAVICON-->`` (head).
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

SPLASH_MARKER = "<!--S2:SPLASH-->"
FAVICON_MARKER = "<!--S2:FAVICON-->"
_FRAGMENT = Path(__file__).with_name("splash.html")


def fragment() -> str:
    """The splash's style, markup and script (the file's leading comment left out). Read each time: it is small,
    and an edited page shows after a reload without a restart, like browser_host.html."""
    html = _FRAGMENT.read_text(encoding="utf-8")
    return re.sub(r"\A\s*<!--.*?-->\s*", "", html, count=1, flags=re.S)


def _static_mark() -> str:
    """The mark's SVG without the animation classes and the sheen (for the favicon)."""
    svg = re.search(r"<svg id=\"s2-splash-mark\".*?</svg>", fragment(), re.S).group(0)  # type: ignore[union-attr]
    svg = re.sub(r"<g clip-path=.*?</g>", "", svg, flags=re.S)
    svg = re.sub(r'\s(?:id="s2-splash-mark"|class="[^"]*"|aria-hidden="true")', "", svg)
    svg = re.sub(r"<clipPath.*?</clipPath>|<linearGradient id=\"s2m-shine\".*?</linearGradient>", "", svg, flags=re.S)
    svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    return re.sub(r">\s+<", "><", svg)


def favicon_link() -> str:
    return f'<link rel="icon" href="data:image/svg+xml,{urllib.parse.quote(_static_mark(), safe=" =:/\'")}">'


def expand(html: str) -> str:
    """``html`` with the splash and favicon markers filled in (each once)."""
    if SPLASH_MARKER in html:
        html = html.replace(SPLASH_MARKER, fragment(), 1)
    if FAVICON_MARKER in html:
        html = html.replace(FAVICON_MARKER, favicon_link(), 1)
    return html
