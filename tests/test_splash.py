"""Offline checks of the SAS SpeedyStore Studio splash (logo + name + status while the Studio opens): one fragment in
the Workspace page, the browser host page and the desktop shortcut's starting.html; small, self-contained, and still
when the user asks for reduced motion."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from support import APPS_DIR

from singlestore_mcp import workspace_app
from singlestore_mcp.apps import splash
from singlestore_mcp.apps._core import build_page

_EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//|@import|url\(\s*["']?https?:""", re.I)


def test_fragment_small_and_self_contained() -> None:
    frag = splash.fragment()
    assert len(frag.encode("utf-8")) < 8000, "the splash should stay a few KB"
    assert not _EXTERNAL.search(frag), "no external assets or fonts"
    assert "S2:SPLASH" not in frag and not frag.lstrip().startswith("<!--")
    # Reduced motion: no animation (the defaults are the finished logo); presenter mode: bigger.
    assert re.search(r"prefers-reduced-motion:\s*reduce\)\s*\{[^}]*animation:\s*none", frag)
    assert "html[data-presenter] #s2-splash" in frag
    # Light and dark: colours come from the page tokens with light-dark() fallbacks.
    assert "light-dark(" in frag


def test_favicon_is_the_static_mark() -> None:
    link = splash.favicon_link()
    assert link.startswith('<link rel="icon" href="data:image/svg+xml,')
    assert "sp-streak" not in link and "s2m-shine" not in link and "clip-path" not in link


def test_workspace_page_has_the_splash_once() -> None:
    html = build_page("workspace.html")
    assert "<!--S2:SPLASH-->" not in html
    assert html.count('id="s2-splash"') == 1 and "window.S2Splash" in html
    # Before the module scripts (the logo shows at first paint).
    assert html.index('id="s2-splash"') < html.index('id="root"')


def test_host_page_has_the_splash_and_favicon() -> None:
    html = splash.expand((APPS_DIR / "browser_host.html").read_text(encoding="utf-8"))
    assert "S2:SPLASH" not in html and "S2:FAVICON" not in html
    assert html.count('id="s2-splash"') == 1 and 'rel="icon"' in html


def test_starting_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_app, "data_dir", lambda: tmp_path)
    page = workspace_app._splash("http://127.0.0.1:1/abc/?tool=sql_editor").read_text(encoding="utf-8")
    assert "S2:SPLASH" not in page and "S2:FAVICON" not in page and "__TARGET__" not in page
    assert 'id="s2-splash"' in page and 'rel="icon"' in page and "#s2-continue" in page
    assert '"http://127.0.0.1:1/abc/?tool=sql_editor"' in page
