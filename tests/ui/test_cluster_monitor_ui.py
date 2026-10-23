"""Cluster Monitor Health, History and Backups tabs in a real browser (browser view of the test server).
Read-only on the cluster."""

from __future__ import annotations

from typing import Any

import pytest
from uihelpers import app_frame, banners, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]

# A tab's read has ended: its button is enabled again (it is disabled, with a spinner, while the read runs).
_BUTTON_IDLE = "(() => {{ const b = [...document.querySelectorAll('button')].filter((b) => b.innerText.trim() === {label!r});" \
               " return b.length > 0 && b.every((x) => !x.disabled); }})()"
_HISTORY_IDLE = _BUTTON_IDLE.format(label="Sample now")
_BACKUPS_IDLE = _BUTTON_IDLE.format(label="Refresh")


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
    for _ in range(2):
        f.get_by_role("button", name="Sample now").click()
        assert wait_js(f, _HISTORY_IDLE, 30), "Sample now didn't finish: " + text(f)[-300:]
    assert wait_js(f, "/last sample/.test(document.body.innerText) || /sampling is off/.test(document.body.innerText)", 30)
    # Export CSV: one row per point and node, with a header.
    f.evaluate("window.__csv = null; S2.download = (name, text) => { window.__csv = [name, text]; return true; }")
    f.get_by_role("button", name="Export CSV").click()
    name, csv = wait_js(f, "window.__csv", 10)
    lines = csv.strip().split("\r\n")
    assert name.startswith("cluster-history-1h-") and name.endswith(".csv"), name
    assert lines[0].startswith("time_utc,node_id,host,cpu_pct,") and len(lines) > 1, lines[:3]
    f.get_by_role("button", name="24 hours").click()
    assert wait_js(f, "/One point per 5 min/.test(document.body.innerText)", 30), text(f)[-400:]
    f.get_by_role("button", name="7 days").click()
    assert wait_js(f, "/One point per 30 min/.test(document.body.innerText)", 30), text(f)[-400:]
    assert f.evaluate("document.querySelectorAll('.cm-io-charts .cm-chart').length") == 6

    _tab(f, "Backups")
    assert wait_js(f, "/Per database \\(\\d+\\)/.test(document.body.innerText)", 30), text(f)[:400]
    f.get_by_role("button", name="1 day", exact=True).click()
    # The re-read with the new limit (the old table stays shown meanwhile, so wait for the read itself to end).
    assert wait_js(f, _BACKUPS_IDLE, 30), "Backups re-read didn't finish: " + text(f)[:300]

    # Regression: this click was lost now and then (no .cm-node after 10 s), when a re-render (auto-refresh, a tab's
    # data arriving) landed between its mousedown and mouseup; the app now holds renders while a button is pressed.
    _tab(f, "Overview")
    assert wait_js(f, "document.querySelector('[role=tab][aria-selected=true]')?.innerText.startsWith('Overview')", 10), \
        "the Overview tab wasn't selected: " + text(f)[:200]
    assert wait_js(f, "document.querySelectorAll('.cm-node').length > 0 && !!document.querySelector('.cm-chart svg')", 10)
    b = banners(page)
    assert not b, f"error banner shown: {b}"
    e = errors.drain()
    assert not e, f"console errors: {e}"
