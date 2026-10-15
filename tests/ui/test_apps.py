"""UI regression of the Workspace and the standalone apps in a real browser (Edge by default), served by the
test server's browser view. Read-only on the cluster (a DELETE is typed but only to check it asks first)."""

from __future__ import annotations

import re
import time
import urllib.parse
import warnings
from typing import Any

import pytest
from playwright.sync_api import expect
from support import DB, McpSession
from uihelpers import app_frame, banner, banners, find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]

WS_VIEWS = [
    ("Notebook", re.compile(r"^Notebook")), ("Schema Explorer", re.compile(r"Schema Explorer")),
    ("Pipeline Monitor", re.compile(r"Pipeline Monitor")), ("Cluster Monitor", re.compile(r"Cluster Monitor")),
    ("Query History", re.compile(r"Query History")), ("Alerts", re.compile(r"^Alerts")),
    ("Connections", re.compile(r"^Connections")), ("Workload", re.compile(r"^Workload")),
    ("SQL Editor", re.compile(r"^SQL Editor$")),
]
_LOADED = "!document.querySelector('.s2-spinner, .ws-loading') && document.body.innerText.length > 80"


def _open(page: Any, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")


def _no_problems(page: Any, errors: Any, what: str) -> None:
    b = banners(page)
    assert not b, f"{what}: error banner shown: {b}"
    e = errors.drain()
    assert not e, f"{what}: console errors: {e}"


# ----------------------------------------------------------------------------- 1. Workspace: every view

@pytest.fixture
def workspace(ui, app_url):
    page, errors = ui
    _open(page, app_url("sql_editor", {"database": DB, "view": "sql"}))
    ws = find_frame(page, "SingleStore Workspace", 30)
    find_frame(page, "SQL Editor", 30)
    return page, errors, ws


def test_workspace_every_view_loads(workspace) -> None:
    page, errors, ws = workspace
    failures = []
    for label, rx in WS_VIEWS:
        ws.locator(f'nav.ws-rail button[aria-label^="{label}"]').click()
        try:
            f = find_frame(page, rx, 25)
            if not wait_js(f, _LOADED, 25):
                failures.append(f"{label}: still loading / empty")
                continue
            time.sleep(1.0)
            if banner(f):
                failures.append(f"{label}: banner {banner(f)[:300]}")
            e = errors.drain()
            if e:
                failures.append(f"{label}: console errors {e}")
            current = ws.locator(f'nav.ws-rail button[aria-label^="{label}"]').get_attribute("aria-current")
            if current != "page":
                failures.append(f"{label}: rail button not marked current ({current})")
        except AssertionError as exc:
            failures.append(f"{label}: {exc}")
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------------- 2. SQL Editor in the Workspace

def _run_sql(frame: Any, sql: str) -> None:
    frame.evaluate("(sql) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: {sql, database: 'SASDP', run: true}}))", sql)


def test_sql_editor_query_chart_resize_error_confirm(workspace) -> None:
    page, errors, ws = workspace
    sql = find_frame(page, "SQL Editor")
    _run_sql(sql, "SELECT Make, COUNT(*) AS n, ROUND(AVG(MSRP)) AS avg_msrp FROM CARS GROUP BY Make ORDER BY n DESC")
    assert wait_js(sql, r"document.body.innerText.includes('Toyota') && /\d+ rows?/.test(document.body.innerText)", 30), \
        "query result grid didn't show rows: " + text(sql)[-400:]

    sql.get_by_role("button", name="Chart", exact=True).click()
    assert wait_js(sql, "document.querySelectorAll('svg rect, svg path').length > 5", 10), "Chart view drew nothing"

    # Regression: resizing showed "ResizeObserver loop completed with undelivered notifications" as an error banner.
    for w, h in [(900, 700), (1600, 1000), (700, 500), (1280, 800), (1000, 1100), (1400, 900)]:
        page.set_viewport_size({"width": w, "height": h})
        time.sleep(0.4)
    time.sleep(1.0)
    _no_problems(page, errors, "after resizing with the chart shown")

    sql.get_by_role("button", name="Table", exact=True).click()
    _run_sql(sql, "SELECT * FROM zz_no_such_table")
    assert wait_js(sql, "document.body.innerText.includes(\"doesn't exist\")", 20), "SQL error not shown"
    assert not banner(sql), "a SQL error must show in the results, not as a page error"

    _run_sql(sql, "DELETE FROM CARS WHERE 1 = 0")
    assert wait_js(sql, r"/confirm|changes data|Run anyway/i.test(document.body.innerText)", 10), \
        "write statement didn't ask for confirmation: " + text(sql)[-300:]
    sql.get_by_role("button", name="Cancel", exact=True).click()

    _run_sql(sql, "SELECT '100%' AS pct, 'æøå 你好' AS txt, NULL AS nothing")
    assert wait_js(sql, "document.body.innerText.includes('100%') && document.body.innerText.includes('你好') && document.body.innerText.includes('æøå')", 20), \
        "% / unicode result not shown"
    _no_problems(page, errors, "SQL editor")


def test_sql_editor_select_ticked_columns(workspace) -> None:
    """Ticking columns in the schema tree turns the table's "SELECT columns" button into "SELECT (n)", which inserts
    a SELECT of just those columns (in table order) at the cursor, without running it."""
    page, errors, ws = workspace
    sql = find_frame(page, "SQL Editor")
    table = sql.locator('.sq-row.tbl:has(span.name:text-is("CARS"))').first
    db_row = sql.locator(f'.sq-row:not(.tbl):has(span.name:text-is("{DB}"))').first
    expect(db_row).to_be_visible(timeout=20_000)  # the tree has loaded
    # The current database opens by itself once its tables arrive; open it only if it hasn't.
    if not wait_js(sql, "document.querySelectorAll('.sq-row.tbl').length > 0", 8):
        db_row.locator("span.name").click()
    expect(table).to_be_visible(timeout=20_000)
    # Expand to show the columns with checkboxes. Click the name: hovering the row reveals its action buttons,
    # and a click on the row's centre can land on one of them.
    table.locator("span.name").click()
    for col in ("Model", "Make"):  # ticked out of order: the SELECT lists them in table order
        sql.get_by_label(f"Pick column {col}", exact=True).check()
    button = table.get_by_role("button", name="SELECT (2)", exact=True)
    expect(button).to_be_visible()  # shown without hovering once columns are ticked
    button.click()
    inserted = wait_js(sql, """(() => {
        const t = document.querySelector('.cm-content')?.innerText ?? '';
        const last = t.slice(t.lastIndexOf('SELECT'));
        return /FROM/.test(last) && last;
    })()""", 10)
    assert inserted, "no SELECT inserted: " + text(sql)[-300:]
    assert re.search(r"Make[\s\S]*Model", inserted) and "MSRP" not in inserted, f"wrong column list: {inserted!r}"
    assert "rows" not in (sql.locator(".sq-results").inner_text() if sql.locator(".sq-results").count() else ""), \
        "inserting the SELECT must not run it"
    _no_problems(page, errors, "SQL editor column picking")


def test_sql_editor_save_as_filter_keeps_focus(workspace) -> None:
    """Typing in Save as's filter keeps the cursor there (it used to jump to the file name after one character)."""
    page, errors, ws = workspace
    sql = find_frame(page, "SQL Editor")
    sql.locator(".sq-filebtns").get_by_role("button", name="Save as", exact=True).click()
    flt = sql.get_by_label("Filter", exact=True)
    expect(flt).to_be_visible(timeout=20_000)
    flt.click()
    flt.press_sequentially("abc", delay=80)
    assert flt.input_value() == "abc", f"filter holds {flt.input_value()!r}"
    assert wait_js(sql, "document.activeElement?.getAttribute('aria-label') === 'Filter'", 3), "focus left the filter"
    assert "abc" not in sql.get_by_label("File name").input_value(), "typing went into the file name"
    sql.get_by_role("button", name="Cancel", exact=True).first.click()
    _no_problems(page, errors, "SQL editor Save as filter")


def test_sql_editor_streams_a_big_result(workspace) -> None:
    """LIMIT 100000: the first rows show before the rest has been read (the ROADMAP's "fetches all rows before
    showing any"), the total appears when loading ends, scrolling loads more rows into the grid, and Export CSV
    writes every row."""
    page, errors, ws = workspace
    sql = find_frame(page, "SQL Editor")
    _run_sql(sql, "SELECT a.Make, a.Model, a.MSRP, b.Make AS make2 FROM CARS a, CARS b LIMIT 100000")
    assert wait_js(sql, "document.querySelectorAll('.sq-out tbody tr').length >= 1000", 60), "no first page: " + text(sql)[-300:]
    assert wait_js(sql, r"/\b100,000 rows\b/.test(document.querySelector('.sq-status').innerText)", 120), \
        "the total didn't show when loading ended: " + sql.locator(".sq-status").inner_text()
    assert "loading" not in sql.locator(".sq-status").inner_text()
    shown = sql.evaluate("document.querySelectorAll('.sq-out tbody tr').length")
    assert shown < 100_000, "the grid should hold only the pages scrolled to"
    for _ in range(3):
        sql.evaluate("(() => { const o = document.querySelector('.sq-out'); o.scrollTop = o.scrollHeight; o.dispatchEvent(new Event('scroll')); })()")
        time.sleep(0.3)
    assert wait_js(sql, f"document.querySelectorAll('.sq-out tbody tr').length > {shown}", 20), "scrolling didn't load more rows"
    sql.evaluate("window.__csv = null; S2.download = (name, text) => { window.__csv = text; return true; }")
    sql.get_by_role("button", name="Export CSV", exact=True).click()
    lines = wait_js(sql, "window.__csv && window.__csv.split('\\r\\n').length", 120)
    assert lines == 100_001, f"Export CSV should hold the header and all 100,000 rows, got {lines}"
    _no_problems(page, errors, "SQL Editor streamed result")


def test_sql_editor_stop_while_loading(workspace) -> None:
    page, errors, ws = workspace
    sql = find_frame(page, "SQL Editor")
    _run_sql(sql, "SELECT a.*, b.Model AS m2 FROM CARS a, CARS b LIMIT 150000")
    stop = sql.locator(".sq-status").get_by_role("button", name="Stop", exact=True)
    try:
        stop.click(timeout=30_000)
    except Exception:  # noqa: BLE001 - it finished before Stop could be clicked
        pytest.skip("the result loaded before Stop could be pressed")
    assert wait_js(sql, r"/\bstopped\b/.test(document.querySelector('.sq-status').innerText)", 20), \
        "Stop didn't stop loading: " + sql.locator(".sq-status").inner_text()
    status = sql.locator(".sq-status").inner_text()
    assert "loading" not in status and "150,000 rows" not in status
    _no_problems(page, errors, "SQL Editor Stop")


def test_schema_explorer_distribution(workspace) -> None:
    """Skew: the Distribution tab of a table and the database's Check skew; nothing is read until asked."""
    page, errors, ws = workspace
    ws.locator('nav.ws-rail button[aria-label^="Schema Explorer"]').click()
    se = find_frame(page, re.compile(r"Schema Explorer"), 25)
    se.evaluate("(db) => window.parent.postMessage({type: 's2-open-view', view: 'schema', database: db, table: 'CARS'}, '*')", DB)
    tab = se.get_by_role("tab", name="Distribution")
    if not wait_js(se, "[...document.querySelectorAll('[role=tab]')].some((t) => t.innerText === 'Distribution')", 20):
        row = se.locator('.se-row.tbl:has(.se-name:text-is("CARS"))').first
        if not row.count():
            se.locator(f'.se-row.db:has(.se-name:text-is("{DB}"))').first.click()
        se.locator('.se-row.tbl:has(.se-name:text-is("CARS"))').first.click()
    expect(tab).to_be_visible(timeout=20_000)
    assert not re.search("row variation", text(se), re.I), "skew must not be read before the tab is opened"
    tab.click()
    assert wait_js(se, "/Row variation/i.test(document.body.innerText) && /Rows per partition/i.test(document.body.innerText)", 30), \
        "Distribution tab didn't show the partitions: " + text(se)[-400:]
    assert re.search("shard key", text(se), re.I) and "Detecting and resolving data skew" in text(se)
    # The database overview: Check skew lists every table, worst first.
    se.locator(f'.se-row.db:has(.se-name:text-is("{DB}"))').first.click()
    se.get_by_role("button", name="Check skew", exact=True).click()
    assert wait_js(se, r"/Skew: \d+ table\(s\) high/.test(document.body.innerText)", 60), "Check skew showed nothing: " + text(se)[-400:]
    _no_problems(page, errors, "Schema Explorer distribution")


def _visible_sql(page: Any, timeout: float = 30, ready: bool = True) -> Any:
    """The SQL Editor frame of the tab that is shown (each SQL tab is its own frame; the others are hidden).
    ``ready``: wait until it has restored its draft (its help then mentions the tab shortcuts)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for f in page.frames:
            try:
                if f.evaluate("document.title") != "SQL Editor" or f.frame_element().get_attribute("hidden") is not None:
                    continue
                if not ready or f.evaluate("document.body.innerText.includes('opens a new tab')"):
                    return f
            except Exception:  # noqa: BLE001 - detached while switching tabs
                continue
        time.sleep(0.3)
    raise AssertionError("no visible, restored SQL Editor tab")


def _editor_text(frame: Any) -> str:
    return frame.evaluate("document.querySelector('.cm-content')?.innerText ?? ''")


def _type_at_end(page: Any, frame: Any, text_: str) -> None:
    frame.locator(".cm-content").click()
    page.keyboard.press("Control+End")
    page.keyboard.type(text_)
    page.keyboard.press("Escape")  # an autocomplete popup


def _tab_ids(ws: Any) -> list[str]:
    return ws.evaluate("[...document.querySelectorAll('.ws-tabs .ws-tab')].map((t) => t.dataset.tab)")


def _active_tab(ws: Any) -> str | None:
    return ws.evaluate("document.querySelector('.ws-tabs .ws-tab.on')?.dataset.tab ?? null")


def _wait_saved(cluster: McpSession, tab_id: str, marker: str) -> None:
    _, secs = cluster.wait_until("sql_editor_state", {"tab_id": tab_id},
                                 lambda d: marker in ((d.get("draft") or {}).get("sql") or ""), timeout=20, every=0.5)
    assert secs is not None, f"tab {tab_id}: {marker!r} wasn't autosaved"


def test_sql_editor_tabs(workspace, cluster: McpSession, app_url) -> None:
    """SQL tabs: + opens one, each tab keeps its own text, the tabs (order, active tab, text) come back after a
    reload and a server restart, a hand-off from another view lands in the active tab, keyboard shortcuts, and
    closing a tab with unsaved changes asks first."""
    page, errors, ws = workspace
    first = _visible_sql(page)
    wait_js(ws, "document.querySelectorAll('.ws-tabs .ws-tab').length > 0", 15)
    tab1 = _active_tab(ws)
    assert tab1, "no SQL tab bar"
    stamp = str(int(time.time()))
    one, two = f"tab_one_{stamp}", f"tab_two_{stamp}"

    ws.get_by_role("button", name="New SQL tab").click()
    assert wait_js(ws, f"(() => {{ const a = document.querySelector('.ws-tab.on')?.dataset.tab; return a && a !== {tab1!r} && a; }})()", 15), \
        "+ didn't open a new tab"
    tab2 = _active_tab(ws)
    second = _visible_sql(page)
    assert _tab_ids(ws).index(tab2) == _tab_ids(ws).index(tab1) + 1, "the new tab should open right after the current one"
    _type_at_end(page, second, f"SELECT 2 AS {two}")
    _wait_saved(cluster, tab2, two)
    assert wait_js(ws, f"!!document.querySelector('.ws-tab[data-tab=\"{tab2}\"] .dirty')", 10), "unsaved text should show ● on the tab"

    ws.locator(f'.ws-tab[data-tab="{tab1}"] button.name').click()
    first = _visible_sql(page)
    _type_at_end(page, first, f"\nSELECT 1 AS {one}")
    _wait_saved(cluster, tab1, one)
    assert one in _editor_text(first) and two not in _editor_text(first)
    assert two in _editor_text(second) and one not in _editor_text(second)

    # Hand-off from another view (as Query History's "Open in SQL Editor" does): into the active tab, below its text.
    ws.locator('nav.ws-rail button[aria-label^="Schema Explorer"]').click()
    schema = find_frame(page, re.compile(r"Schema Explorer"), 25)
    handoff = f"SELECT 3 AS handoff_{stamp}"
    schema.evaluate("(sql) => window.parent.postMessage({type: 's2-open-view', view: 'sql', sql, database: 'SASDP'}, '*')", handoff)
    assert wait_js(ws, f"document.querySelector('.ws-tab.on')?.dataset.tab === {tab1!r}", 10), "hand-off didn't open the active SQL tab"
    first = _visible_sql(page)
    assert wait_js(first, f"document.querySelector('.cm-content').innerText.includes('handoff_{stamp}')", 10), "hand-off SQL not in the active tab"
    assert f"handoff_{stamp}" not in _editor_text(second)
    assert _editor_text(first).index(one) < _editor_text(first).index(f"handoff_{stamp}"), "hand-off must go below the tab's text"
    _wait_saved(cluster, tab1, f"handoff_{stamp}")

    # Shortcuts (forwarded by the editor to the tab bar): Ctrl+Tab goes to the next tab.
    first.locator(".cm-content").click()
    page.keyboard.press("Control+Tab")
    assert wait_js(ws, f"document.querySelector('.ws-tab.on')?.dataset.tab === {tab2!r}", 10), "Ctrl+Tab didn't switch tabs"
    _no_problems(page, errors, "SQL tabs before reload")

    # Reload: same tabs, same order, same active tab, each with its own text.
    order = _tab_ids(ws)
    page.reload(wait_until="domcontentloaded")
    ws = find_frame(page, "SingleStore Workspace", 30)
    assert wait_js(ws, f"document.querySelectorAll('.ws-tabs .ws-tab').length === {len(order)}", 30), "tabs didn't come back"
    assert _tab_ids(ws) == order and _active_tab(ws) == tab2
    assert two in _editor_text(_visible_sql(page))
    ws.locator(f'.ws-tab[data-tab="{tab1}"] button.name').click()
    assert one in _editor_text(_visible_sql(page))
    _no_problems(page, errors, "SQL tabs after reload")

    # Server restart (the state is on disk): the old browser link stops working, a new one shows the same tabs.
    restarted = cluster.call("restart_server", timeout=120)
    if not restarted.is_error and (restarted.data or {}).get("restarted"):
        cluster.ok("list_databases", timeout=60)
        page.goto(app_url("sql_editor", {"database": DB, "view": "sql"}), wait_until="domcontentloaded")
        errors.drain()  # the old page's polls failed while the server restarted: expected
        ws = find_frame(page, "SingleStore Workspace", 30)
        assert wait_js(ws, f"document.querySelectorAll('.ws-tabs .ws-tab').length === {len(order)}", 30), "tabs didn't come back after restart"
        assert _tab_ids(ws) == order and _active_tab(ws) == tab1
        assert one in _editor_text(_visible_sql(page))
    else:
        warnings.warn(f"server not restarted, restart part skipped: {restarted.text[:200]}", stacklevel=1)

    # Ctrl+Alt+N: a new tab; an empty tab closes with one click.
    _visible_sql(page).locator(".cm-content").click()
    page.keyboard.press("Control+Alt+n")
    assert wait_js(ws, f"document.querySelectorAll('.ws-tabs .ws-tab').length === {len(order) + 1}", 15), "Ctrl+Alt+N didn't open a tab"
    tab3 = _active_tab(ws)
    _visible_sql(page)
    ws.locator(f'.ws-tab[data-tab="{tab3}"] button.x').click()
    assert wait_js(ws, f"!document.querySelector('.ws-tab[data-tab=\"{tab3}\"]')", 10), "an empty tab should close at once"

    # Closing a tab with unsaved changes asks first (a second click on "Discard ×").
    ws.locator(f'.ws-tab[data-tab="{tab2}"] button.name').click()
    _visible_sql(page)
    ws.locator(f'.ws-tab[data-tab="{tab2}"] button.x').click()
    armed = ws.locator(f'.ws-tab[data-tab="{tab2}"] button.x.armed')
    expect(armed).to_be_visible(timeout=5000)
    assert tab2 in _tab_ids(ws), "the tab closed without asking"
    armed.click()
    assert wait_js(ws, f"!document.querySelector('.ws-tab[data-tab=\"{tab2}\"]')", 10), "Discard × didn't close the tab"
    assert tab2 not in [t["id"] for t in cluster.ok("sql_editor_tabs")["tabs"]]
    assert _active_tab(ws) in order
    time.sleep(1.0)
    _no_problems(page, errors, "SQL tabs")


# ----------------------------------------------------------------------------- 3. Query History (standalone)

def test_query_history_copy_tabs_trends(ui, app_url) -> None:
    page, errors = ui
    url = app_url("query_history")
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    _open(page, url)
    qh = find_frame(page, "Query History", 30)
    rows = qh.locator(".qh-table tbody tr")
    rows.first.wait_for(timeout=30_000)
    if rows.count() == 0:
        pytest.skip("no queries in the history")
    rows.first.click()
    copy_btn = qh.get_by_role("button", name="Copy SQL", exact=True)
    expect(copy_btn).to_be_enabled(timeout=15_000)  # enabled once the query's details have loaded
    copy_btn.click()
    toast = qh.locator("#s2-toasts .s2-toast").last
    toast.wait_for(timeout=5000)
    # Regression: the toast said "Couldn't copy" in the browser view.
    assert "Copied" in toast.inner_text() and "Couldn't" not in toast.inner_text(), toast.inner_text()
    try:
        clip = page.evaluate("navigator.clipboard.readText()")
    except Exception as exc:  # noqa: BLE001 - clipboard reads may be refused in some setups
        clip = None
        warnings.warn(f"couldn't read the clipboard to compare the copied SQL: {exc}", stacklevel=1)
    if clip is not None:
        shown = " ".join(rows.first.locator("td.sql").inner_text().split()).rstrip("…. ")
        assert clip.strip(), "clipboard is empty after Copy SQL"
        assert " ".join(clip.split()).startswith(shown[:25]), f"clipboard {clip[:80]!r} doesn't match the row {shown[:80]!r}"

    for tab in ("Trends", "Advisor", "Queries"):
        qh.get_by_role("tab", name=tab).click()
        time.sleep(2.5)
        assert not banner(qh), f"{tab} tab: {banner(qh)}"

    qh.get_by_role("tab", name="Trends").click()
    bar = qh.locator("svg rect.pick").last
    bar.wait_for(timeout=20_000)
    bar.dispatch_event("click")
    assert wait_js(qh, r"document.getElementById('qh-trend-runs') && /slowest first|No runs|no runs/i.test(document.body.innerText)", 20), \
        "clicking a Trends bar didn't list its queries"
    _no_problems(page, errors, "Query History")


# ----------------------------------------------------------------------------- 4. Workload (standalone)

UI_WL = "zz test ui"


@pytest.fixture
def ui_workload(cluster: McpSession):
    cluster.ok("workload_define", {"name": UI_WL, "database": DB, "concurrency": 2, "duration_s": 40, "statements": [
        {"id": "count", "name": "Count", "sql": "SELECT COUNT(*) FROM CARS", "per_minute": 120},
        {"id": "pick", "name": "By origin", "sql": "SELECT * FROM CARS WHERE Origin = '{{pick:Asia|Europe|USA}}'", "per_minute": 60}]})
    base = cluster.ok("workload_run", {"name": UI_WL, "label": "ui baseline", "duration_s": 5})
    cluster.wait_until("workload_status", {"run_id": base["id"]}, lambda d: isinstance(d, dict) and d.get("state") not in ("starting", "running"), 60)
    yield UI_WL
    running = (cluster.call("workload_list").data or {}).get("running")
    if running:
        cluster.call("workload_stop", {"run_id": running["id"]})
    cluster.call("workload_delete", {"name": UI_WL, "runs_too": True})


def test_workload_run_stop_compare(ui, app_url, ui_workload) -> None:
    page, errors = ui
    _open(page, app_url("workload", {"name": ui_workload}))
    wl = find_frame(page, "Workload", 30, min_text=40)
    sel = wl.locator('select[aria-label="Workload"]')
    sel.wait_for()
    if sel.input_value() != ui_workload:
        sel.select_option(ui_workload)
    # Regression: the statement boxes stayed empty (only the placeholder showed) because a textarea ignores a
    # value attribute.
    boxes = wl.locator(".wl-stmt textarea")
    expect(boxes.first).to_have_value("SELECT COUNT(*) FROM CARS")
    expect(boxes.nth(1)).to_have_value("SELECT * FROM CARS WHERE Origin = '{{pick:Asia|Europe|USA}}'")
    wl.locator("#wl-run").click()
    # Live status comes from workload_status through the browser view (it was blocked there once).
    assert wait_js(wl, "document.querySelector('.wl-progress') && /running/.test(document.body.innerText)", 20), \
        "no live run status: " + text(wl)[-500:]
    assert wait_js(wl, r"(() => { const k = [...document.querySelectorAll('.wl-kpi')].find(e => /executions/.test(e.innerText)); return k && /[1-9]/.test(k.querySelector('.v').innerText); })()", 15), \
        "executions didn't update while running"
    assert "not available in the browser view" not in text(wl)
    wl.get_by_role("button", name="Stop", exact=True).click()
    assert wait_js(wl, "!document.querySelector('.wl-progress') && /\\bstopped\\b/.test(document.body.innerText)", 20), \
        "run didn't show as stopped: " + text(wl)[-400:]
    _no_problems(page, errors, "Workload run/stop")

    wl.get_by_role("button", name="Compare", exact=True).first.click()
    selects = wl.locator("label.wl-field select")
    selects.first.wait_for()
    assert selects.count() >= 2
    selects.nth(0).select_option(index=1)
    selects.nth(1).select_option(index=0)
    wl.locator("button.s2-btn.primary", has_text="Compare").click()
    assert wait_js(wl, "/p50 before → after/.test(document.body.innerText)", 20), "comparison not shown: " + text(wl)[-400:]
    _no_problems(page, errors, "Workload compare")


def _workload_named(cluster: McpSession, name: str) -> dict | None:
    return next((w for w in cluster.ok("workload_list")["workloads"] if w["name"] == name), None)


def _save_workload(wl: Any) -> None:
    """Click the Workload view's Save and wait until the app has saved (its "unsaved" badge is gone), before the
    test reads the store: polling workload_list while the save replaced workloads.json made that replace fail
    on Windows (file in use), which was the flaky "Save didn't store the added statement"."""
    wl.locator("#wl-save").click()
    saved = wait_js(wl, r"!/\bunsaved\b/.test(document.querySelector('.wl-main h2')?.innerText ?? 'unsaved')", 15)
    toasts = wl.evaluate("[...document.querySelectorAll('#s2-toasts .s2-toast')].map((t) => t.innerText).join(' | ')")
    assert saved, f"Save didn't finish in the Workload view (toasts: {toasts!r})"


@pytest.fixture
def editor_workload(cluster: McpSession):
    name = "zz test ui editor"
    cluster.ok("workload_define", {"name": name, "database": DB, "statements": [
        {"id": "s1", "name": "Count", "sql": "SELECT COUNT(*) FROM CARS", "per_minute": 60}]})
    yield name
    cluster.call("workload_delete", {"name": name, "runs_too": True})


def test_sql_editor_and_workload_round_trip(workspace, cluster: McpSession, editor_workload: str) -> None:
    """SQL Editor "→ Workload" adds the selected statement to a workload (unsaved, in the Workload view); the
    Workload view's "Open in SQL Editor" opens a statement in a new tab whose "Update in workload" writes the
    edited SQL back (and saves); with the workload gone, the tab says so and offers "→ Workload" instead."""
    page, errors, ws = workspace
    stamp = str(int(time.time()))
    sql = _visible_sql(page)
    sent = f"SELECT Make FROM CARS WHERE Cylinders = {{{{int:4:8}}}} /* zz_towl_{stamp} */"
    sql.evaluate("(sql) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: {sql, database: 'SASDP'}}))", sent)
    assert wait_js(sql, f"document.querySelector('.cm-content').innerText.includes('zz_towl_{stamp}')", 10)

    sql.get_by_role("button", name="→ Workload", exact=True).first.click()
    panel = sql.locator(".sq-towl")
    panel.locator('select[aria-label="Workload"]').select_option(editor_workload)
    panel.locator('input[aria-label="Runs per minute"]').fill("30")
    panel.get_by_role("button", name="Add to workload", exact=True).click()

    assert wait_js(ws, "document.querySelector('nav.ws-rail button[aria-label^=\"Workload\"]')?.getAttribute('aria-current') === 'page'", 15), \
        "→ Workload didn't switch to the Workload view"
    wl = find_frame(page, "Workload", 30, min_text=40)
    added = f"[...document.querySelectorAll('.wl-stmt textarea')].findIndex((t) => t.value.includes('zz_towl_{stamp}'))"
    assert wait_js(wl, f"{added} === 1", 20), "statement not added to the workload: " + text(wl)[-400:]
    assert "unsaved" in text(wl), "added statements should be unsaved until Save"
    assert wl.locator('.wl-stmt input[aria-label="Runs per minute"]').nth(1).input_value() == "30"
    _save_workload(wl)
    _, secs = cluster.wait_until("workload_list", {}, lambda d: any(w["name"] == editor_workload and len(w["statements"]) == 2 for w in d["workloads"]), 15, 0.5)
    assert secs is not None, "Save didn't store the added statement"
    _no_problems(page, errors, "SQL Editor → Workload")

    # Workload → SQL Editor: a new tab with the statement, which can write it back.
    tabs_before = _tab_ids(ws)
    wl.locator(".wl-stmt").first.get_by_role("button", name="Open in SQL Editor", exact=True).click()
    assert wait_js(ws, f"document.querySelectorAll('.ws-tabs .ws-tab').length === {len(tabs_before) + 1}", 15), "no new SQL tab"
    new_tab = _active_tab(ws)
    assert new_tab not in tabs_before
    try:
        editor = _visible_sql(page)
        assert wait_js(editor, "document.querySelector('.cm-content').innerText.trim() === 'SELECT COUNT(*) FROM CARS'", 15), \
            "the new tab doesn't hold the statement: " + _editor_text(editor)
        update = editor.get_by_role("button", name="Update in workload", exact=True)
        expect(update).to_be_visible(timeout=10_000)
        _wait_saved(cluster, new_tab, "SELECT COUNT(*) FROM CARS")
        assert (cluster.ok("sql_editor_state", {"tab_id": new_tab})["draft"].get("origin") or {}).get("statement_id") == "s1", \
            "the tab's draft doesn't remember the workload statement"

        editor.locator(".cm-content").click()
        page.keyboard.press("Control+a")
        page.keyboard.type(f"SELECT COUNT(*) AS zz_upd_{stamp} FROM CARS")
        page.keyboard.press("Escape")
        update.click()
        _, secs = cluster.wait_until("workload_list", {}, lambda d: any(w["name"] == editor_workload and f"zz_upd_{stamp}" in w["statements"][0]["sql"]
                                                                      for w in d["workloads"]), 15, 0.5)
        assert secs is not None, "Update in workload didn't save the edited SQL"
        # The open Workload view shows it too.
        assert wait_js(wl, f"document.querySelector('.wl-stmt textarea').value.includes('zz_upd_{stamp}')", 10), "Workload view not updated"

        cluster.ok("workload_delete", {"name": editor_workload, "runs_too": True})
        update.click()
        assert wait_js(editor, "document.body.innerText.includes('Workload statement gone')", 10), "a deleted workload should be reported"
        expect(editor.get_by_role("button", name="→ Workload instead", exact=True)).to_be_visible()
        _no_problems(page, errors, "Workload → SQL Editor")
    finally:
        cluster.call("sql_editor_tab_close", {"tab_id": new_tab})


@pytest.fixture
def chat_workload(cluster: McpSession):
    name = "zz test ui chat"
    cluster.ok("workload_define", {"name": name, "database": DB, "statements": [
        {"id": "s1", "name": "Count", "sql": "SELECT COUNT(*) FROM CARS", "per_minute": 60},
        {"id": "s2", "name": "Makes", "sql": "SELECT Make FROM CARS", "per_minute": 6}]})
    yield name
    cluster.call("workload_delete", {"name": name, "runs_too": True})


CANNED_ANSWER = """Two statements to try.
```sql
-- name: zz chat variant
-- per_minute: 12
SELECT COUNT(*) AS zz_chat FROM CARS WHERE Cylinders = {{int:4:8}}
```
And a replacement:
```sql
SELECT Make, AVG(MSRP) AS zz_replaced FROM CARS GROUP BY Make
```"""


def test_workload_chat_buttons(ui, app_url, cluster: McpSession, chat_workload: str) -> None:
    """The Workload view's Claude panel: an answer (sent the way Claude's chat answers arrive, sql_editor_reply)
    gets Add to workload / Replace statement N / Test / Copy on each SQL block."""
    page, errors = ui
    url = app_url("workload", {"name": chat_workload})
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    _open(page, url)
    wl = find_frame(page, "Workload", 30, min_text=40)
    sel = wl.locator('select[aria-label="Workload"]')
    sel.wait_for()
    if sel.input_value() != chat_workload:
        sel.select_option(chat_workload)
    wl.locator("button.wl-claude").click()
    panel = wl.locator("aside.wl-chat")
    expect(panel).to_be_visible()
    chat_id = wait_js(wl, "document.querySelector('aside.wl-chat')?.dataset.chatId", 10)
    assert chat_id, "chat panel has no chat id"
    cluster.ok("sql_editor_reply", {"editor_id": chat_id, "message": CANNED_ANSWER})
    blocks = panel.locator(".wl-code")
    expect(blocks).to_have_count(2, timeout=20_000)

    blocks.nth(0).get_by_role("button", name="Add to workload", exact=True).click()
    boxes = wl.locator(".wl-stmt textarea")
    expect(boxes).to_have_count(3)
    expect(boxes.nth(2)).to_have_value("SELECT COUNT(*) AS zz_chat FROM CARS WHERE Cylinders = {{int:4:8}}")
    expect(wl.locator('.wl-stmt input[aria-label="Statement name"]').nth(2)).to_have_value("zz chat variant")
    expect(wl.locator('.wl-stmt input[aria-label="Runs per minute"]').nth(2)).to_have_value("12")
    assert "unsaved" in text(wl)

    second = panel.locator(".wl-code").nth(1)
    second.locator('select[aria-label="Statement to replace"]').select_option("1")
    second.get_by_role("button", name="Replace statement 2", exact=True).click()
    expect(boxes.nth(1)).to_have_value("SELECT Make, AVG(MSRP) AS zz_replaced FROM CARS GROUP BY Make")

    first = panel.locator(".wl-code").nth(0)
    first.get_by_role("button", name="Test", exact=True).click()
    assert wait_js(wl, r"/\d+\+? rows in/.test(document.querySelector('aside.wl-chat .wl-code .res')?.innerText ?? '')", 30), \
        "Test didn't show rows and time: " + text(wl)[-300:]

    first.get_by_role("button", name="Copy", exact=True).click()
    toast = wl.locator("#s2-toasts .s2-toast").last
    expect(toast).to_contain_text("Copied")

    _save_workload(wl)
    _, secs = cluster.wait_until("workload_list", {}, lambda d: any(
        w["name"] == chat_workload and len(w["statements"]) == 3 and "zz_replaced" in w["statements"][1]["sql"]
        and w["statements"][2]["per_minute"] == 12 for w in d["workloads"]), 15, 0.5)
    assert secs is not None, f"Save didn't keep the chat's changes: {_workload_named(cluster, chat_workload)}"
    _no_problems(page, errors, "Workload chat")


# ----------------------------------------------------------------------------- 5. Every standalone app

STANDALONE = [
    ("schema_explorer", {"database": DB, "table": "CARS"}, "MSRP"),
    ("query_grid", {"sql": "SELECT * FROM CARS", "database": DB}, "428 rows"),
    ("pipeline_monitor", {}, "pipeline"),
    ("cluster_monitor", {}, "node"),
    ("query_history", {}, "ms"),
    ("alerts", {}, "Rule"),
    ("notebook", {"database": DB}, "SQL"),
    ("connections_window", {}, "New connection"),
    ("workload", {}, "Workload"),
    ("sql_editor", {"database": DB}, "SQL"),
]


@pytest.mark.parametrize("tool,args,expect", STANDALONE, ids=[s[0] for s in STANDALONE])
def test_standalone_app_opens(ui, app_url, tool: str, args: dict, expect: str) -> None:
    page, errors = ui
    _open(page, app_url(tool, args))
    f = app_frame(page, 30)
    assert wait_js(f, f"new RegExp({expect!r}, 'i').test(document.body.innerText)", 30), \
        f"{tool}: expected {expect!r} in: {text(f)[:300]}"
    time.sleep(1.0)
    _no_problems(page, errors, tool)

