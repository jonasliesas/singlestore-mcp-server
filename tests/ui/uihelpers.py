"""Helpers for the UI tests: the apps run in nested sandboxed srcdoc iframes (browser host page -> app
frame -> one iframe per Workspace view), so frames are found by document.title."""

from __future__ import annotations

import re
import time
from typing import Any

IGNORED_CONSOLE = re.compile(r"favicon|ResizeObserver loop", re.I)


class Errors:
    """Console errors and uncaught exceptions from every frame of a page."""

    def __init__(self, page: Any) -> None:
        self.items: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.items.append(f"pageerror: {str(e)[:400]}"))

    def _console(self, m: Any) -> None:
        if m.type == "error":
            url = (m.location or {}).get("url") or ""
            self.items.append(f"console.error: {m.text[:400]}" + (f" [{url[:200]}]" if url else ""))

    def drain(self) -> list[str]:
        out = [e for e in self.items if not IGNORED_CONSOLE.search(e)]
        self.items.clear()
        return out


def frame_title(frame: Any) -> tuple[str, int]:
    """(title, length of the page's text); the Studio splash's logo and status don't count as the page's text."""
    try:
        return frame.evaluate("[document.title, document.body ? document.body.innerText.length"
                              " - (document.getElementById('s2-splash')?.innerText.length ?? 0) : 0]")
    except Exception:  # noqa: BLE001 - detached / navigating
        return "", 0


def find_frame(page: Any, title: str | re.Pattern, timeout: float = 20, min_text: int = 1) -> Any:
    """The newest frame whose document.title matches ``title`` (a regex or an exact string) and has text."""
    rx = title if isinstance(title, re.Pattern) else re.compile(f"^{re.escape(title)}$")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = [f for f in page.frames if (lambda t: rx.search(t[0]) and t[1] >= min_text)(frame_title(f))]
        if hits:
            return hits[-1]
        time.sleep(0.3)
    raise AssertionError(f"no frame titled {rx.pattern!r}; frames: {[frame_title(f)[0] for f in page.frames]}")


def app_frame(page: Any, timeout: float = 30) -> Any:
    """The app frame of a standalone browser-view page (the host page itself is titled "SAS SpeedyStore Studio" or "<tool> · SAS SpeedyStore Studio")."""
    return find_frame(page, re.compile(r"^(?!SingleStore$).+"), timeout, min_text=40)


def banner(frame: Any) -> str:
    try:
        return frame.evaluate("document.getElementById('s2-page-error')?.textContent || ''")
    except Exception:  # noqa: BLE001
        return ""


def banners(page: Any) -> dict[str, str]:
    """Error banners in every frame of the page, by frame title."""
    out = {}
    for f in page.frames:
        b = banner(f)
        if b:
            out[frame_title(f)[0] or f.url[:60]] = b[:600]
    return out


def text(frame: Any) -> str:
    return frame.evaluate("document.body.innerText")


def wait_js(frame: Any, expr: str, timeout: float = 20) -> Any:
    """Poll a JS expression in a frame until it is truthy; returns the value (None on timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            v = frame.evaluate(expr)
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
    return None
