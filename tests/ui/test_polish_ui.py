"""Design-review polish round (studio-design-review.md, 2026-10-30): empty states (B-2, L-4), number formatting and the
# column in the SQL result grid (V-5), sticky error toasts (I-2), text cues next to colours (A-5), arrow keys in tab
bars (I-4), tree actions on keyboard focus (I-3), one danger style (V-2), one button size in Security (V-3), stat cards
that aren't cut off (L-2), one SAS session state (C-3) and renders held while a button is pressed. Read-only on the cluster."""

from __future__ import annotations

import re
from typing import Any

import pytest
from support import DB, McpSession
from uihelpers import app_frame, banners, find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]
WS = "SAS SpeedyStore Studio · Workspace"


def _sql_tab(page: Any, app_url, **args: Any) -> Any:
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, **args}), wait_until="domcontentloaded")
    find_frame(page, WS, 40)
    sql = find_frame(page, "SQL Editor", 40)
    assert wait_js(sql, "document.querySelector('.cm-content') && document.querySelector('.sq-run')", 30)
    return sql


def _set_sql(frame: Any, sql: str, run: bool = True) -> None:
    frame.evaluate("(d) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: d}))", {"sql": sql, "run": run, "database": DB})


# ----------------------------------------------------------------------------- B-2 / L-4 empty states

def test_sql_editor_example_queries(ui, app_url) -> None:
    """An empty tab shows example queries to click (the cars table, rows per table) and the shortcuts folded away;
    a click puts the SQL in the editor and runs it."""
    page, errors = ui
    sql = _sql_tab(page, app_url)
    assert wait_js(sql, "[...document.querySelectorAll('.sq-example')].some((b) => b.textContent === 'Top 10 cars by MSRP')", 30), text(sql)[-600:]
    labels = sql.evaluate("[...document.querySelectorAll('.sq-example')].map((b) => b.textContent)")
    assert f"Rows per table in {DB}" in labels, labels
    # The keyboard hints are still there, folded into "Keyboard shortcuts".
    assert sql.evaluate("document.querySelector('details.sq-keys summary')?.textContent") == "Keyboard shortcuts"
    assert not sql.evaluate("document.querySelector('details.sq-keys').open")

    sql.locator(".sq-example", has_text="Top 10 cars by MSRP").click()
    assert wait_js(sql, "document.querySelectorAll('.sq-out tbody tr').length === 10", 40), text(sql)[-600:]
    assert "ORDER BY MSRP DESC" in sql.evaluate("document.querySelector('.cm-content').innerText")

    _set_sql(sql, "", run=False)
    assert not banners(page), banners(page)


def test_workload_empty_state(ui, app_url, cluster: McpSession) -> None:
    """No workload yet: three steps, From history as the primary button, and a sample workload on CARS (not saved)."""
    if cluster.ok("workload_list").get("workloads"):
        pytest.skip("the test home already has workloads (another test's are still there)")
    page, errors = ui
    page.goto(app_url("workload"), wait_until="domcontentloaded")
    wl = app_frame(page, 30)
    assert wait_js(wl, "document.querySelector('.wl-empty .wl-steps')", 30), text(wl)[:500]
    assert wl.locator(".wl-steps li").count() == 3
    assert wl.locator(".wl-empty .s2-btn.primary").inner_text().strip() == "From history"
    wl.get_by_role("button", name="Try a sample workload").click()
    assert wait_js(wl, "document.querySelectorAll('.wl-stmt').length === 3", 30), text(wl)[-600:]
    assert "FROM CARS" in wl.locator(".wl-stmt textarea").first.input_value()
    assert wl.get_by_role("button", name=re.compile(r"Save and run on SingleStore")).count() == 1
    # Nothing was saved.
    assert not cluster.ok("workload_list").get("workloads")
    assert not banners(page), banners(page)


# ----------------------------------------------------------------------------- V-5 numbers in result grids

_COPY_ROW = """(sel) => {
  const row = document.querySelector(sel);
  const r = document.createRange(); r.selectNodeContents(row);
  const s = document.getSelection(); s.removeAllRanges(); s.addRange(r);
  const dt = new DataTransfer();
  row.dispatchEvent(new ClipboardEvent('copy', { clipboardData: dt, bubbles: true, cancelable: true }));
  s.removeAllRanges();
  return dt.getData('text/plain');
}"""


def test_sql_result_numbers_and_row_numbers(ui, app_url) -> None:
    """Numeric columns show thousands separators (not for year / id columns, every digit kept), right-aligned, with
    a # column; the title and a copied selection keep the raw values."""
    page, errors = ui
    sql = _sql_tab(page, app_url)
    _set_sql(sql, "SELECT 1234567 AS n, 2024 AS sale_year, CAST('12345678901234567890.25' AS DECIMAL(30,2)) AS big, 'abc' AS t"
                  " UNION ALL SELECT 5, 1999, 1, 'd'")
    assert wait_js(sql, "document.querySelectorAll('.sq-out tbody tr').length === 2", 40), text(sql)[-600:]
    grp = sql.evaluate("new Intl.NumberFormat().formatToParts(12345.6).find((p) => p.type === 'group').value")
    dec = sql.evaluate("new Intl.NumberFormat().formatToParts(12345.6).find((p) => p.type === 'decimal').value")
    head = sql.evaluate("[...document.querySelectorAll('.sq-out thead th')].map((t) => t.firstChild.textContent)")
    assert head == ["#", "n", "sale_year", "big", "t"], head
    cells = sql.evaluate("[...document.querySelectorAll('.sq-out tbody tr:first-child td')].map((t) => [t.textContent, t.title, t.className])")
    assert cells[0][0] == "1" and "s2-rn" in cells[0][2]
    assert cells[1][0] == f"1{grp}234{grp}567" and cells[1][1] == "1234567" and "num" in cells[1][2]
    assert cells[2][0] == "2024"
    assert cells[3][0] == f"12{grp}345{grp}678{grp}901{grp}234{grp}567{grp}890{dec}25", cells[3]
    assert sql.evaluate("getComputedStyle(document.querySelector('.sq-out tbody td.num')).textAlign") == "right"
    copied = sql.evaluate(_COPY_ROW, ".sq-out tbody tr:first-child")
    assert copied == "1234567\t2024\t12345678901234567890.25\tabc", copied
    # Sorting renumbers the rows as shown.
    sql.locator(".sq-out thead th", has_text="n").first.click()
    assert wait_js(sql, "document.querySelector('.sq-out tbody tr:first-child td:nth-child(2)')?.textContent === '5'", 10)
    assert sql.evaluate("document.querySelector('.sq-out tbody tr:first-child td').textContent") == "1"
    assert not banners(page), banners(page)


def test_query_grid_numbers(ui, app_url) -> None:
    """The Query Grid formats numbers the same way and copies raw values."""
    page, errors = ui
    page.goto(app_url("query_grid", {"sql": "SELECT 1234567 AS n, 2024 AS model_year", "database": DB}), wait_until="domcontentloaded")
    qg = app_frame(page, 30)
    assert wait_js(qg, "document.querySelector('.qg-grid tbody td.num')", 30), text(qg)[:400]
    grp = qg.evaluate("new Intl.NumberFormat().formatToParts(12345.6).find((p) => p.type === 'group').value")
    cells = qg.evaluate("[...document.querySelectorAll('.qg-grid tbody tr:first-child td')].map((t) => t.textContent)")
    assert cells == ["1", f"1{grp}234{grp}567", "2024"], cells
    assert qg.evaluate(_COPY_ROW, ".qg-grid tbody tr:first-child") == "1234567\t2024"


# ----------------------------------------------------------------------------- I-2 toasts, A-5 text next to colour

def test_error_toasts_stay_until_dismissed(ui, app_url) -> None:
    """Info toasts hide after 3.5 s; error toasts stay (role=alert, "Error:" in words) until their ✕ is clicked."""
    page, errors = ui
    page.goto(app_url("cluster_config"), wait_until="domcontentloaded")
    cf = app_frame(page, 30)
    cf.evaluate("() => { S2.toast('zz info toast'); S2.toast('zz error toast', 'err'); }")
    toasts = "[...document.querySelectorAll('#s2-toasts .s2-toast')].map((t) => t.innerText)"
    shown = cf.evaluate(toasts)
    assert any("zz info toast" in t for t in shown) and any("zz error toast" in t for t in shown), shown
    err = cf.locator("#s2-toasts .s2-toast.err")
    assert err.get_attribute("role") == "alert"
    assert err.inner_text().startswith("Error:")
    assert wait_js(cf, f"!{toasts}.some((t) => t.includes('zz info toast'))", 6), cf.evaluate(toasts)
    page.wait_for_timeout(4000)  # past the old 7 s limit for errors
    assert any("zz error toast" in t for t in cf.evaluate(toasts))
    err.get_by_role("button", name="Dismiss").click()
    assert wait_js(cf, f"!{toasts}.some((t) => t.includes('zz error toast'))", 3)


def test_colour_signals_have_text(ui, app_url) -> None:
    """Config's changed variables carry a "changed" chip (not only the coloured bar); Security's red / yellow flags
    carry ▲ / ● and say the severity to screen readers."""
    page, errors = ui
    page.goto(app_url("cluster_config"), wait_until="domcontentloaded")
    cf = app_frame(page, 30)
    assert wait_js(cf, "document.querySelectorAll('.cf-table tbody tr.row').length > 10", 40), text(cf)[:400]
    missing = cf.evaluate("[...document.querySelectorAll('.cf-table tr.changed:not(.differs)')].filter((r) => !r.querySelector('.cf-chg')).length")
    assert missing == 0
    page.goto(app_url("security"), wait_until="domcontentloaded")
    sc = app_frame(page, 30)
    assert wait_js(sc, "document.querySelector('.sc-flags')", 40), text(sc)[:400]
    flags = sc.evaluate("[...document.querySelectorAll('.sc-flags .s2-badge')].map((b) => [b.className, b.textContent])")
    assert flags, "no flagged users to check"
    for cls, label in flags:
        if " err" in cls:
            assert label.startswith("▲") and "High risk:" in label, label
        elif " warn" in cls:
            assert label.startswith("●") and "Worth a look:" in label, label
