"""SAS SpeedyStore Studio design round (2026-10-26): where code runs (engine chips), the Open / Save as dialog's
keyboard behaviour, presenter mode (bigger text, identifying metadata masked in every view, query results left as they
are), the Studio's own queries hidden, and the contrast of the changed colours in the browser. Read-only on the cluster."""

from __future__ import annotations

import getpass
import re
import time
from typing import Any

import pytest
from support import DB, McpSession
from uihelpers import banners, find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]
WS = "SAS SpeedyStore Studio · Workspace"


def _set_sql(frame: Any, sql: str, run: bool = True, **extra: Any) -> None:
    frame.evaluate("(d) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: d}))", {"sql": sql, "run": run, "database": DB, **extra})


def _rail(ws: Any, label: str) -> None:
    # A JS click: Playwright's hit test is off inside CSS-zoomed frames (presenter mode).
    ws.locator(f'nav.ws-rail button[aria-label^="{label}"]').first.evaluate("e => e.click()")


def _open(page: Any, url: str) -> tuple[Any, Any]:
    page.goto(url, wait_until="domcontentloaded")
    ws = find_frame(page, WS, 40)
    sql = find_frame(page, "SQL Editor", 40)
    assert wait_js(sql, "document.querySelector('.cm-content') && document.querySelector('.sq-run')", 30)
    return ws, sql


def _active(mcp: McpSession) -> str:
    return mcp.ok("connections_state")["active"]


# ----------------------------------------------------------------------------- where it runs

def test_where_it_runs_singlestore(ui, app_url, cluster: McpSession) -> None:
    """SQL Editor, Notebook, Workload and Query History say in words where SQL runs: on SingleStore, through the active
    connection (before, while and after a run, and in the run history)."""
    page, errors = ui
    conn = _active(cluster)
    ws, sql = _open(page, app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- where it runs"}))
    run_label = f"Run on SingleStore · {conn}"
    assert wait_js(sql, f"document.querySelector('.sq-run')?.getAttribute('aria-label') === {run_label!r}", 15), \
        sql.locator(".sq-run").get_attribute("aria-label")
    assert "Run on SingleStore" in sql.locator(".sq-run").inner_text()
    # "Run as" has a visible label.
    assert sql.locator("label.sq-runas-label").inner_text().strip().startswith("Run as")
    # The editor's corner badge is gone (it covered the end of the first line).
    assert sql.locator(".sq-editor > .sq-lang").count() == 0

    _set_sql(sql, "SELECT Make, Model FROM CARS LIMIT 3")
    chip = ".sq-status .s2-engine[data-engine='s2']"
    assert wait_js(sql, f"/^Ran /.test(document.querySelector(\"{chip}\")?.getAttribute('aria-label') ?? '')", 40), text(sql)[-500:]
    assert sql.locator(chip).get_attribute("aria-label") == f"Ran on SingleStore (connection {conn})"
    assert f"SingleStore · {conn}" in sql.locator(chip).inner_text().replace(" ", " ")

    # A slow statement: the status line says where it runs while it runs.
    sql.evaluate("() => { window.__running = ''; new MutationObserver(() => { const t = document.querySelector('.sq-status')?.innerText ?? '';"
                 " if (/Running/.test(t)) window.__running = t; }).observe(document.querySelector('.sq-status'), {subtree: true, childList: true, characterData: true}); }")
    _set_sql(sql, "SELECT SLEEP(2) AS s")
    assert wait_js(sql, "window.__running", 20), "no running status seen"
    assert "Running on SingleStore" in sql.evaluate("window.__running")

    # Run history: every entry has the engine and the connection.
    sql.locator(".sq-head").get_by_role("button", name=re.compile(r"^History")).evaluate("e => e.click()")
    assert wait_js(sql, "document.querySelector('.sq-hist .sq-hitem .s2-engine[data-engine=\"s2\"]')", 15), text(sql)[-500:]
    first = sql.locator(".sq-hist .sq-hitem .s2-engine").first.get_attribute("aria-label")
    assert first == f"Ran on SingleStore (connection {conn})", first

    # Notebook: every code cell says where it runs.
    _rail(ws, "Notebook")
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    assert wait_js(nb, "document.querySelector('.cell.sql .bar .s2-engine[data-engine=\"s2\"]') && document.querySelector('.cell.python .bar .s2-engine[data-engine=\"python\"]')", 20), text(nb)[-400:]
    assert f"SingleStore · {conn}" in nb.locator(".cell.sql .bar .s2-engine").first.inner_text().replace(" ", " ")
    assert "Python (local)" in nb.locator(".cell.python .bar .s2-engine").first.inner_text()
    # Labelled kernel buttons (not ■ / ↻ glyphs) and one "Ask Claude".
    assert nb.get_by_role("button", name="Restart kernel", exact=True).count() == 1
    assert nb.get_by_role("button", name="Interrupt the running cell", exact=True).count() == 1
    assert nb.locator(".s2-ask").first.inner_text().strip() == "Ask Claude"

    # Workload: the Run button names the engine.
    _rail(ws, "Workload")
    wl = find_frame(page, re.compile(r"^Workload"), 30, min_text=40)
    wl.evaluate("() => [...document.querySelectorAll('button')].find((b) => /^\\s*New\\s*$/.test(b.textContent))?.click()")
    if wait_js(wl, "document.getElementById('wl-run')", 10):
        assert re.search(r"(Run|Save and run) on SingleStore", wl.locator("#wl-run").inner_text())

    # Query History: the Studio's own queries are hidden by default, with a switch to show them.
    _rail(ws, "Query History")
    qh = find_frame(page, re.compile(r"Query History"), 60)
    assert wait_js(qh, "[...document.querySelectorAll('label')].some((l) => /Show Studio's own queries/.test(l.textContent))", 60), text(qh)[-400:]
    assert not banners(page), banners(page)


def test_cluster_queries_hide_own(ui, app_url) -> None:
    """Cluster → Queries hides the Studio's own statements (this monitor's process-list read, SELECT @@…) unless asked."""
    page, errors = ui
    page.goto(app_url("cluster_monitor"), wait_until="domcontentloaded")
    cm = find_frame(page, re.compile(r"Cluster Monitor"), 40)
    assert wait_js(cm, "document.querySelector('[role=tab]')", 30)
    cm.get_by_role("tab", name=re.compile(r"^Queries")).click()
    toggle = cm.get_by_label(re.compile(r"Show Studio's own queries"))
    assert wait_js(cm, "[...document.querySelectorAll('label')].some((l) => /Show Studio's own queries/.test(l.textContent))", 20)
    toggle.check()
    # With the switch on, the monitor's own process-list read (marked /* s2-cluster-monitor */) shows up.
    assert wait_js(cm, "document.body.innerText.includes('s2-cluster-monitor') || document.body.innerText.includes('MV_PROCESSLIST')", 20), \
        text(cm)[-600:]
    toggle.uncheck()
    assert wait_js(cm, "!document.body.innerText.includes('s2-cluster-monitor')", 20)


@pytest.mark.sas
def test_where_it_runs_sas(ui, app_url, sas: McpSession) -> None:
    """PROC SQL runs in SAS Compute, FedSQL in CAS in CAS; the Run button, the running status and the result say so,
    with the SAS Viya connection; a run on a SingleStore library shows the pushdown outcome next to it."""
    page, errors = ui
    viya = sas.ok("sas_connections_state")["active"]
    sas.call("sas_libraries", {}, timeout=300)  # warm session
    ws, sql = _open(page, app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- where it runs, SAS"}))
    sql.get_by_label("Run as", exact=True).select_option("procsql")
    assert wait_js(sql, f"document.querySelector('.sq-run')?.getAttribute('aria-label') === {f'Run in SAS Compute · {viya}'!r}", 10), \
        sql.locator(".sq-run").get_attribute("aria-label")
    sql.evaluate("() => { window.__running = ''; new MutationObserver(() => { const t = document.querySelector('.sq-status')?.innerText ?? '';"
                 " if (/Running|Waiting/.test(t)) window.__running += t + '\\n'; }).observe(document.querySelector('.sq-status'), {subtree: true, childList: true, characterData: true}); }")
    _set_sql(sql, "select name, age from sashelp.class where age > 14;")
    chip = ".sq-status .s2-engine[data-engine='compute']"
    assert wait_js(sql, f"document.querySelector(\"{chip}\") && /Result/.test(document.querySelector('.sq-status').innerText)", 120), text(sql)[-600:]
    assert sql.locator(chip).get_attribute("aria-label") == f"Ran in SAS Compute (connection {viya})"
    assert "Running in SAS Compute" in sql.evaluate("window.__running"), sql.evaluate("window.__running")

    # A SingleStore library: "SAS Compute → pushed down to SingleStore: …" next to the chip.
    s2libs = [x for x in sas.ok("sas_libraries")["libraries"] if x["singlestore"]]
    tables = sas.ok("sas_tables", {"library": s2libs[0]["name"]})["tables"] if s2libs else []
    if tables:
        _set_sql(sql, f"select count(*) as n from {s2libs[0]['name']}.{tables[0]['name']};")
        assert wait_js(sql, "document.querySelector('.sq-status .s2-pushdown')", 120), text(sql)[-600:]
        chip_text = " ".join(sql.locator(".sq-status .s2-pushdown").inner_text().split())
        assert re.search(r"pushed down to SingleStore: (full|partial|none)", chip_text), chip_text

    # CAS: FedSQL in CAS.
    sql.get_by_label("Run as", exact=True).select_option("cas")
    assert wait_js(sql, f"document.querySelector('.sq-run')?.getAttribute('aria-label') === {f'Run in CAS · {viya}'!r}", 10)
    _set_sql(sql, "select 1 as one from (select 1 as x) t;")
    chip = ".sq-status .s2-engine[data-engine='cas']"
    assert wait_js(sql, f"/^Ran /.test(document.querySelector(\"{chip}\")?.getAttribute('aria-label') ?? '')", 180), text(sql)[-600:]
    assert sql.locator(chip).get_attribute("aria-label") == f"Ran in CAS (connection {viya})"
    # The run history keeps the engine and the Viya connection.
    sql.locator(".sq-head").get_by_role("button", name=re.compile(r"^History")).evaluate("e => e.click()")
    assert wait_js(sql, "document.querySelector('.sq-hist .sq-hitem .s2-engine[data-engine=\"cas\"]')", 15), text(sql)[-500:]
    assert sql.locator(".sq-hist .sq-hitem .s2-engine").first.get_attribute("aria-label") == f"Ran in CAS (connection {viya})"
    sql.get_by_label("Run as", exact=True).select_option("singlestore")


# ----------------------------------------------------------------------------- the file dialog

def _dialog_keyboard(page: Any, frame: Any, panel: str, open_button: Any) -> None:
    open_button.click()
    assert wait_js(frame, f"(() => {{ const p = document.querySelector('{panel}'); return p && !p.hidden && p.getAttribute('role') === 'dialog'"
                          f" && p.getAttribute('aria-modal') === 'true'; }})()", 10), "not a modal dialog"
    # The focus is in the dialog at once (the filter), and Tab never leaves it.
    assert wait_js(frame, f"document.querySelector('{panel}').contains(document.activeElement)", 5), "focus not in the dialog"
    for _ in range(25):
        page.keyboard.press("Tab")
        assert frame.evaluate(f"document.querySelector('{panel}').contains(document.activeElement)"), "Tab left the dialog"
    for _ in range(5):
        page.keyboard.press("Shift+Tab")
        assert frame.evaluate(f"document.querySelector('{panel}').contains(document.activeElement)"), "Shift+Tab left the dialog"
    # Escape anywhere closes it, also with the focus outside the dialog.
    frame.evaluate("document.activeElement?.blur()")
    page.keyboard.press("Escape")
    assert wait_js(frame, f"document.querySelector('{panel}').hidden && !document.querySelector('{panel}').hasAttribute('aria-modal')", 5), \
        "Escape didn't close the dialog"


def test_file_dialogs_keyboard(ui, app_url) -> None:
    """Open / Save as in the SQL Editor and the Notebook: role=dialog + aria-modal, focus inside, Tab trapped, Escape
    closes it wherever the focus is; a rename prompt's Escape only cancels the prompt."""
    page, errors = ui
    ws, sql = _open(page, app_url("sql_editor", {"database": DB, "view": "sql"}))
    files = sql.locator(".sq-filebtns")
    _dialog_keyboard(page, sql, ".sq-files", files.get_by_role("button", name="Open", exact=True))
    _dialog_keyboard(page, sql, ".sq-files", files.get_by_role("button", name="Save as", exact=True))
    # Save as: the file name has the focus; Escape in it closes the dialog once (no second close).
    files.get_by_role("button", name="Save as", exact=True).click()
    name = sql.get_by_label("File name", exact=True)
    assert wait_js(sql, "document.activeElement?.getAttribute('aria-label') === 'File name'", 10)
    name.press("Escape")
    assert wait_js(sql, "document.querySelector('.sq-files').hidden", 5)

    _rail(ws, "Notebook")
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    assert wait_js(nb, "document.querySelector('.nb-head')", 20)
    head = nb.locator(".nb-head")
    _dialog_keyboard(page, nb, ".nb-files", head.get_by_role("button", name="Open", exact=True))
    _dialog_keyboard(page, nb, ".nb-files", head.get_by_role("button", name="Save as", exact=True))
    assert not banners(page), banners(page)


# ----------------------------------------------------------------------------- presenter mode

GENERIC = {"root", "admin", "administrator", "sa", "sas", "sasdemo", "test", "user", "public", "default", "localhost"}


def _secrets(mcp: McpSession) -> set[str]:
    """Identifying values that must not show in presenter mode: connection names, hosts, users, SAS Viya addresses, the
    Windows user name."""
    vals: set[str] = set()
    st = mcp.ok("connections_state")
    for c in st["connections"]:
        vals.update([c["name"], c.get("host") or "", c.get("user") or ""])
    try:
        sst = mcp.ok("sas_connections_state")
        for c in sst["connections"]:
            vals.add(c["name"])
            m = re.match(r"https?://([^/:]+)", c.get("url") or "")
            if m:
                vals.add(m.group(1))
            if c.get("user"):
                vals.add(c["user"])
    except AssertionError:
        pass
    vals.add(getpass.getuser())
    return {v for v in vals if v and len(v) >= 3 and v.lower() not in GENERIC}


VISIBLE_TEXT = """() => {
  const out = [document.body.innerText];
  for (const el of document.querySelectorAll('[title], [aria-label], [placeholder]'))
    for (const a of ['title', 'aria-label', 'placeholder']) if (el.hasAttribute(a) && !el.closest('.cm-editor, [data-s2-data]')) out.push(el.getAttribute(a));
  return out.join('\\n');
}"""


def _leaks(frame: Any, secrets: set[str]) -> list[str]:
    try:
        blob = frame.evaluate(VISIBLE_TEXT)
    except Exception:  # noqa: BLE001 - a frame that went away
        return []
    return sorted(s for s in secrets if re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", blob, re.I))


def test_presenter_mode_masks_every_view(ui, app_url, cluster: McpSession) -> None:
    """?present=1: bigger text, and no real connection name, host, user or e-mail in any view (text, tooltips, labels);
    the same value has the same label in every view; query results stay as they are; switching it off restores
    everything."""
    page, errors = ui
    secrets = _secrets(cluster)
    assert secrets, "no identifying values to check"
    ws, sql = _open(page, app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- presenter"}) + "&present=1")
    assert wait_js(ws, "document.documentElement.hasAttribute('data-presenter') && getComputedStyle(document.body).zoom > 1", 10), "no presenter zoom"
    assert ws.locator("nav.ws-rail button.present").get_attribute("aria-pressed") == "true"
    assert wait_js(sql, "document.documentElement.hasAttribute('data-presenter') && !document.documentElement.hasAttribute('data-presenter-loading')", 10)
    conn_label = sql.evaluate("document.querySelector('.sq-run').getAttribute('aria-label')")
    assert re.search(r"· Connection [A-Z]+$", conn_label), conn_label  # "Run on SingleStore · Connection A"

    # Query results stay real: an e-mail address and the active connection's name in a result grid.
    _set_sql(sql, "SELECT 'someone@example.org' AS mail, 'db-7.example.org' AS host")
    assert wait_js(sql, "document.querySelector('.sq-out [data-s2-data] td')", 40), text(sql)[-400:]
    grid = sql.locator(".sq-out").inner_text()
    assert "someone@example.org" in grid and "db-7.example.org" in grid, grid

    leaks: dict[str, list[str]] = {}
    views = [("Schema", r"Schema Explorer"), ("Notebook", r"^Notebook"), ("Pipeline Monitor", r"Pipeline Monitor"),
             ("Cluster Monitor", r"Cluster Monitor"), ("Query History", r"Query History"), ("Security", r"^Security"),
             ("Config", r"^Config"), ("Alerts", r"^Alerts"), ("Workload", r"^Workload"), ("Connections", r"^Connections")]
    leaks["SQL Editor"] = _leaks(sql, secrets)
    leaks["Workspace"] = _leaks(ws, secrets)
    for label, rx in views:
        _rail(ws, label)
        f = find_frame(page, re.compile(rx), 60, min_text=20)
        wait_js(f, "!document.querySelector('.s2-spinner, .ws-loading') && document.body.innerText.length > 60", 40)
        wait_js(f, "!document.documentElement.hasAttribute('data-presenter-loading')", 10)
        time.sleep(2.0)  # late renders and the label round trip
        if label == "Cluster Monitor":
            f.get_by_role("tab", name=re.compile(r"^Queries")).evaluate("e => e.click()")
            time.sleep(1.5)
        leaks[label] = _leaks(f, secrets)
        if label == "Connections":
            body = f.evaluate("document.body.innerText")
            assert re.search(r"Connection [A-Z]\b", body), body[:400]
    leaks = {k: v for k, v in leaks.items() if v}
    assert not leaks, f"identifying values visible in presenter mode: {leaks}"

    # The same connection has the same label in the Connections view and the SQL Editor.
    con = find_frame(page, re.compile(r"^Connections"), 10)
    assert conn_label.split("· ")[-1] in con.evaluate("document.body.innerText")

    # Off again: the real names are back.
    ws.locator("nav.ws-rail button.present").evaluate("e => e.click()")
    assert wait_js(con, "!document.documentElement.hasAttribute('data-presenter')", 10)
    active = _active(cluster)
    assert wait_js(con, f"document.body.innerText.includes({active!r})", 10), "real names not restored"
    assert not banners(page), banners(page)


# ----------------------------------------------------------------------------- contrast in the browser

CONTRAST = """() => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgb = (c) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#fff'; cx.fillRect(0, 0, 1, 1); cx.fillStyle = c; cx.fillRect(0, 0, 1, 1); return [...cx.getImageData(0, 0, 1, 1).data].slice(0, 3); };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const ratio = (a, b) => { const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const bgOf = (el) => { for (let e = el; e; e = e.parentElement) { const b = getComputedStyle(e).backgroundColor; if (b && !/rgba\\(0, 0, 0, 0\\)|transparent/.test(b)) return b; } return getComputedStyle(document.body).backgroundColor; };
  const out = {};
  for (const [name, sel] of [['run button', '.sq-run'], ['engine chip', '.s2-engine'], ['Run as label', '.sq-runas-label']]) {
    const el = document.querySelector(sel); if (!el) continue;
    out[name] = Math.round(ratio(getComputedStyle(el).color, bgOf(el)) * 100) / 100;
  }
  return out;
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_contrast_in_the_browser(browser: Any, app_url, scheme: str) -> None:
    """The Run button (white on the primary fill), the engine chip and the Run as label: >= 4.5:1 in both themes, measured
    on the rendered page."""
    ctx = browser.new_context(color_scheme=scheme, viewport={"width": 1400, "height": 900})
    try:
        page = ctx.new_page()
        ws, sql = _open(page, app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- contrast"}))
        _set_sql(sql, "SELECT 1 AS one")
        assert wait_js(sql, "document.querySelector('.sq-status .s2-engine')", 40)
        got = sql.evaluate(CONTRAST)
        assert set(got) == {"run button", "engine chip", "Run as label"}, got
        low = {k: v for k, v in got.items() if v < 4.5}
        assert not low, f"{scheme}: contrast under 4.5:1: {got}"
    finally:
        ctx.close()


def test_editor_and_notebook_fit_at_1024(ui, app_url) -> None:
    """At 1024×700 the SQL Editor's and the Notebook's headers wrap; the page must not scroll (it did by ~22 px)."""
    page, errors = ui
    page.set_viewport_size({"width": 1024, "height": 700})
    ws, sql = _open(page, app_url("sql_editor", {"database": DB, "view": "sql"}))
    fits = "document.scrollingElement.scrollHeight <= innerHeight + 1"
    assert wait_js(sql, fits, 10), sql.evaluate("[document.scrollingElement.scrollHeight, innerHeight]")
    # The short Run label on narrow widths: "Run · S2", the full target in its accessible name.
    assert sql.locator(".sq-run .short").is_visible() and not sql.locator(".sq-run .long").is_visible()
    assert sql.locator(".sq-run").get_attribute("aria-label").startswith("Run on SingleStore")
    _rail(ws, "Notebook")
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    assert wait_js(nb, "document.querySelector('.nb-head') && document.querySelector('.cell')", 20)
    assert wait_js(nb, fits, 10), nb.evaluate("[document.scrollingElement.scrollHeight, innerHeight]")
