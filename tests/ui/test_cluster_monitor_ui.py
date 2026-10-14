"""Cluster Monitor Health, History and Backups tabs in a real browser (browser view of the test server).
Read-only on the cluster."""

from __future__ import annotations

import time
from typing import Any

import pytest
from uihelpers import app_frame, banners, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]


def _tab(frame: Any, name: str) -> None:
    frame.get_by_role("tab", name=name).click()


def test_cluster_monitor_health_history_backups_tabs(ui, app_url) -> None:
    page, errors = ui
    page.goto(app_url("cluster_monitor", {}), wait_until="domcontentloaded")
    f = app_frame(page, 30)
    assert wait_js(f, "document.querySelectorAll('.cm-node').length > 0", 30), text(f)[:300]
    # Nothing of the new tabs is loaded on open.
    assert not f.evaluate("!!document.querySelector('.cm-hbanner')")

    _tab(f, "Health")
    assert wait_js(f, "!!document.querySelector('.cm-hbanner')", 30), "Health tab: no status banner: " + text(f)[:400]
    body = text(f)
    for part in ("Nodes (", "Databases and partitions", "Events (", "License and version"):
        assert part in body, f"Health tab is missing {part!r}"
    # Every listed problem has a plain explanation and a Docs button.
    issues = f.evaluate("[...document.querySelectorAll('.cm-issue')].map(e => [e.querySelector('.x')?.innerText ?? '', !!e.querySelector('button')])")
    assert all(x and has_btn for x, has_btn in issues), issues
    f.get_by_role("button", name="All", exact=True).click()
    assert wait_js(f, "/Events \\(\\d+\\)/.test(document.body.innerText)", 10)

    _tab(f, "History")
    assert wait_js(f, "/Queued queries/.test(document.body.innerText) && /Connections/.test(document.body.innerText)", 30), text(f)[:400]
    # The test server samples nothing by itself: take one (two readings make a line).
    f.get_by_role("button", name="Sample now").click()
    time.sleep(1.5)
    f.get_by_role("button", name="Sample now").click()
    assert wait_js(f, "/last sample/.test(document.body.innerText) || /sampling is off/.test(document.body.innerText)", 30)
    f.get_by_role("button", name="24 hours").click()
    assert wait_js(f, "/One point per 5 min/.test(document.body.innerText)", 30), text(f)[-400:]
    f.get_by_role("button", name="7 days").click()
    assert wait_js(f, "/One point per 30 min/.test(document.body.innerText)", 30), text(f)[-400:]
    assert f.evaluate("document.querySelectorAll('.cm-io-charts .cm-chart').length") == 6

    _tab(f, "Backups")
    assert wait_js(f, "/Per database \\(\\d+\\)/.test(document.body.innerText)", 30), text(f)[:400]
    f.get_by_role("button", name="1 day", exact=True).click()
    assert wait_js(f, "/Per database \\(\\d+\\)/.test(document.body.innerText)", 30)

    _tab(f, "Overview")
    assert wait_js(f, "document.querySelectorAll('.cm-node').length > 0", 10)
    time.sleep(1.0)
    b = banners(page)
    assert not b, f"error banner shown: {b}"
    e = errors.drain()
    assert not e, f"console errors: {e}"
