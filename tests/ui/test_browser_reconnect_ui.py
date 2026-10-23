"""A browser tab survives a server restart (browser view of a test server of its own, so restarting and
stopping it touches nothing else): the page holds the app's calls, shows "Reconnecting…", carries on when the
server is back; a server gone for good gives a "Session ended" page with all polling stopped, and editor text
that couldn't be autosaved is kept in the tab and saved once a server answers on the same address again."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from support import McpSession, server_env
from uihelpers import app_frame, text, wait_js

pytestmark = pytest.mark.ui

_WATCH_CONN = ("window.__conn = []; new MutationObserver(() => __conn.push(document.documentElement.dataset.conn))"
               ".observe(document.documentElement, { attributes: true, attributeFilter: ['data-conn'] })")


def _env(test_home, sql_dir) -> dict[str, str]:
    # No standby: a restart then leaves the port closed for a few seconds (a fresh server loads), so the page
    # really has to reconnect. A short reconnect time for the "gone for good" part.
    return server_env(test_home, sql_dir, {"SINGLESTORE_MCP_NO_STANDBY": "1", "SINGLESTORE_MCP_BROWSER_RECONNECT": "8"})


def _calls(page: Any) -> list[int]:
    seen: list[int] = []
    page.on("response", lambda r: seen.append(r.status) if r.url.endswith("/call") else None)
    return seen


def _ended(page: Any, timeout: float = 45) -> None:
    assert wait_js(page, "document.documentElement.dataset.conn === 'ended' && /Session ended/.test(document.body.innerText)", timeout), \
        "no 'Session ended' page: " + text(page)[:300]
    assert page.evaluate("!document.querySelector('iframe')"), "the app is still there (and polling)"


@pytest.mark.cluster
def test_cluster_monitor_reconnects_after_restart_and_ends_cleanly(ui, test_home, sql_dir) -> None:
    page, errors = ui
    s = McpSession(_env(test_home, sql_dir), read_timeout=120)
    try:
        page.goto(s.ok("browser_link", {"tool": "cluster_monitor", "arguments": {}})["url"], wait_until="domcontentloaded")
        f = app_frame(page, 30)
        assert wait_js(f, "document.querySelectorAll('.cm-node').length > 0", 45), text(f)[:300]
        page.evaluate(_WATCH_CONN)

        done = threading.Event()
        out: dict[str, Any] = {}

        def restart() -> None:
            out["r"] = s.call("restart_server", timeout=120)
            done.set()

        threading.Thread(target=restart, daemon=True).start()
        while not done.wait(0.2):  # as after a laptop resume: check now (otherwise the next poll notices)
            page.evaluate("dispatchEvent(new Event('online'))")
        r = out["r"]
        assert not r.is_error and r.data["restarted"], r.text
        assert wait_js(page, "document.documentElement.dataset.conn === 'ok' && __conn.includes('reconnecting')", 45), \
            f"didn't reconnect: {page.evaluate('__conn')}"

        # Auto-refresh carries on by itself, against the new server, on the same link.
        seen = _calls(page)
        page.wait_for_timeout(12_000)  # Cluster Monitor refreshes every 5 s (events arrive while Playwright waits)
        assert len(seen) >= 2 and all(c == 200 for c in seen), seen
        body = text(f)
        for bad in ("Lost connection", "Refresh failed", "Reconnecting"):
            assert bad not in body, f"{bad!r} shown after the restart: {body[:400]}"
        assert not page.evaluate("document.getElementById('conn').classList.contains('on')")
    finally:
        s.close()

    # Gone for good: after the reconnect time, a clean end page and no more calls.
    _ended(page)
    seen.clear()
    page.wait_for_timeout(6_000)
    assert not seen, f"calls after the session ended: {seen}"
    assert "Reopen the app from Claude" in text(page)
    errors.drain()  # refused connections while the server was away: expected


def test_unsent_editor_text_is_kept_and_saved_after_reconnect(ui, test_home, sql_dir) -> None:
    page, errors = ui
    env = _env(test_home, sql_dir)
    s = McpSession(env, read_timeout=120)
    try:
        url = s.ok("browser_link", {"tool": "sql_editor", "arguments": {}})["url"]
        page.goto(url, wait_until="domcontentloaded")
        sql = _sql_frame(page)
    finally:
        s.close()  # the server goes away while the editor is open
    marker = f"unsent_{int(time.time())}"
    sql.locator(".cm-content").click()
    page.keyboard.press("Control+End")
    page.keyboard.type(f"\nSELECT '{marker}';")
    page.keyboard.press("Escape")
    _ended(page)
    assert "Unsaved editor text (1)" in text(page), text(page)[:400]
    kept = page.evaluate("JSON.parse(sessionStorage.getItem('s2-unsent-drafts') || '{}')")
    (tab_id, item), = kept.items()
    assert marker in item["args"]["sql"]

    # A server on the same address again (the slot is free once the old client is gone): Try again saves it.
    s2 = McpSession(env, read_timeout=120)
    try:
        assert s2.ok("browser_link", {"tool": "sql_editor", "arguments": {}})["url"] == url
        page.get_by_role("button", name="Try again").click()
        _, secs = s2.wait_until("sql_editor_state", {"tab_id": tab_id},
                                lambda d: marker in ((d.get("draft") or {}).get("sql") or ""), timeout=45, every=0.5)
        assert secs is not None, "the kept editor text wasn't saved after reconnecting"
        assert wait_js(page, "sessionStorage.getItem('s2-unsent-drafts') === null", 20)
        assert marker in _sql_frame(page).evaluate("document.querySelector('.cm-content')?.innerText ?? ''")
    finally:
        s2.close()
    errors.drain()


def _sql_frame(page: Any, timeout: float = 60) -> Any:
    """The visible SQL Editor frame once it restored its draft (autosave is on from then)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for f in page.frames:
            try:
                if (f.evaluate("document.title") == "SQL Editor" and f.frame_element().get_attribute("hidden") is None
                        and f.evaluate("!!document.querySelector('.cm-content')")):
                    time.sleep(2)  # the draft restore (draft.ready) finishes right after the editor shows
                    return f
            except Exception:  # noqa: BLE001 - frames come and go while the workspace loads
                pass
        time.sleep(0.3)
    raise AssertionError(f"no SQL Editor frame: {json.dumps([x.url[:60] for x in page.frames])}")


def test_reset_browser_links_moves_this_tab_and_ends_the_others(ui, browser, test_home, sql_dir) -> None:
    """Connections -> Reset browser links: a new token; the page that asked moves to the new link by itself,
    another tab with the old link shows "Session ended" (refused) instead of failing calls."""
    page, errors = ui
    s = McpSession(_env(test_home, sql_dir), read_timeout=120)
    other = browser.new_page()
    try:
        old = s.ok("browser_link", {"tool": "connections_window", "arguments": {}})["url"]
        other.goto(s.ok("browser_link", {"tool": "alerts", "arguments": {}})["url"], wait_until="domcontentloaded")
        app_frame(other, 30)
        page.goto(old, wait_until="domcontentloaded")
        f = app_frame(page, 30)
        f.get_by_role("button", name="Reset browser links").click()
        f.get_by_role("button", name="Reset", exact=True).click()
        assert wait_js(page, f"location.href !== {old!r} && !location.href.includes({old.split('/')[3]!r})", 20), page.url
        assert wait_js(app_frame(page, 30), "/Reset browser links/.test(document.body.innerText)", 30)
        new = s.ok("browser_link", {"tool": "connections_window", "arguments": {}})["url"]
        assert page.url.split("?")[0] == new.split("?")[0]

        other.evaluate("dispatchEvent(new Event('online'))")  # (else its next heartbeat notices)
        _ended(other, 20)
        assert "isn't valid any more" in text(other)
    finally:
        other.close()
        s.close()
    errors.drain()
