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
