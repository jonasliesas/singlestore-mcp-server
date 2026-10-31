"""SAS round in the browser: notebook SAS cells in the Workspace's SAS session (an older notebook's %%sas cell and
its saved output open as before; the cell says it runs in SAS Compute and the header offers Restart SAS session;
``--sas``: a SAS cell's WORK table and macro variable seen by a Python in SAS cell), "Load into this session" /
"Unload" on a caslib table in the Schema Explorer and the SQL Editor's CAS tree, and the FedSQL subquery check.

SAS writes: WORK only, plus a session-scope load of the small CARS table (dropped again). Caslib: S2_TEST_SS_CASLIB
(default CSASDP)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect
from support import DB, McpSession
from uihelpers import find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster]

SS = os.environ.get("S2_TEST_SS_CASLIB", "CSASDP")
OLD_NB = "zz_pytest_old_sas_cell.ipynb"
SAS_MIME = "application/vnd.s2.sas+json"


def _notebook(page: Any, app_url: Any, **args: Any) -> Any:
    page.goto(app_url("notebook", {"database": DB, **args}), wait_until="domcontentloaded")
    nb = find_frame(page, re.compile(r"^Notebook"), 30)
    assert wait_js(nb, "document.body.dataset.loaded === '1' && document.querySelectorAll('.cell').length > 0", 30), text(nb)[-400:]
    return nb


def _set_code(page: Any, cell: Any, code: str) -> None:
    cell.locator(".cm-content").click()
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(code)


def test_older_notebook_sas_cell_opens_and_runs_in_sas_compute(ui, app_url, sql_dir: Path) -> None:
    """A notebook saved before (a %%sas cell with the kernel's SAS output) opens as a SAS cell with its output; the
    cell now runs in the Workspace's SAS Compute session (engine chip), and the header offers Restart SAS session."""
    page, errors = ui
    saved_out = {"output_type": "display_data", "metadata": {}, "data": {SAS_MIME: {
        "log": "1  data _null_; set work.nope; run;\nERROR: File WORK.NOPE.DATA does not exist.", "listing": "",
        "errors": 1, "warnings": 0, "first_error": "ERROR: File WORK.NOPE.DATA does not exist.", "library": "S2 = SingleStore"},
        "text/plain": "SAS: 1 error(s), 0 warning(s)"}}
    nbjson = {"cells": [{"cell_type": "code", "id": "c1", "metadata": {}, "execution_count": 3, "outputs": [saved_out],
                         "source": ["%%sas\n", "data _null_; set work.nope; run;"]}],
              "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (sql_dir / OLD_NB).write_text(json.dumps(nbjson), encoding="utf-8")
    nb = _notebook(page, app_url, name=OLD_NB)
    cell = nb.locator(".cell.sas")
    expect(cell).to_have_count(1)
    assert cell.locator(".gutter .badge").inner_text() == "SAS"
    expect(cell.locator(".outputs .sas-out")).to_contain_text("1 error")
    expect(cell.locator(".outputs .sas-out div.err")).to_contain_text("WORK.NOPE.DATA does not exist")
    chip = cell.locator(".bar .s2-engine")
    assert chip.get_attribute("data-engine") == "compute" and "SAS Compute" in chip.inner_text()
    # SAS cells share the Workspace's SAS session: the header offers Restart SAS session for them too.
    expect(nb.locator(".nb-head").get_by_role("button", name="Restart SAS session")).to_have_count(1)
    # "+ SAS" is offered, and a new SAS cell gets the same engine.
    nb.locator(".nb-add").last.get_by_role("button", name="+ SAS", exact=True).click()
    expect(nb.locator(".cell.sas")).to_have_count(2)
    assert nb.locator(".cell.sas").last.locator(".bar .s2-engine").get_attribute("data-engine") == "compute"
    assert not errors.drain()


@pytest.mark.sas
def test_sas_cell_work_and_macro_variables_reach_python_in_sas(ui, app_url) -> None:
    page, errors = ui
    nb = _notebook(page, app_url)
    nb.locator(".nb-add").last.get_by_role("button", name="+ SAS", exact=True).click()
    cell = nb.locator(".cell.sas").last
    _set_code(page, cell, "data work.zz_ui_sascell; set sashelp.class; where age > 14; run;\n%let zz_ui_n = 5;\nproc print data=work.zz_ui_sascell; run;")
    cell.locator(".gutter .run").click()
    expect(cell.locator(".status")).to_contain_text("✓", timeout=240_000)
    assert cell.locator(".status .s2-engine").get_attribute("aria-label").startswith("Ran in SAS Compute")
    out = cell.locator(".outputs .sas-out")
    expect(out).to_contain_text("0 errors")
    assert wait_js(nb, "!!document.querySelector('.cell.sas:last-of-type .sas-lst')?.shadowRoot?.querySelector('table')", 10) or \
        nb.evaluate("[...document.querySelectorAll('.cell.sas .sas-lst')].some((e) => e.shadowRoot?.querySelector('table'))"), "no ODS table"
    nb.locator(".nb-add").last.get_by_role("button", name="+ Python in SAS", exact=True).click()
    py = nb.locator(".cell.sas_python").last
    _set_code(page, py, 'print("n =", SAS.symget("zz_ui_n"), len(SAS.sd2df("work.zz_ui_sascell")))')
    py.locator(".gutter .run").click()
    expect(py.locator(".outputs")).to_contain_text("n = 5 5", timeout=240_000)
    assert not errors.drain()


@pytest.fixture
def ss_caslib(sas: McpSession) -> str:
    libs = {x["name"].upper(): x for x in sas.ok("cas_caslibs", timeout=300)["caslibs"]}
    lib = libs.get(SS.upper())
    if not lib or lib.get("singlestore_kind") != "speedystore":
        pytest.skip(f"no SpeedyStore caslib {SS}")
    sas.call("cas_unload_table", {"caslib": lib["name"], "table": "CARS"}, timeout=120)
    yield lib["name"]
    sas.call("cas_unload_table", {"caslib": lib["name"], "table": "CARS"}, timeout=120)


@pytest.mark.sas
def test_schema_explorer_load_into_this_session_and_unload(ui, app_url, ss_caslib: str) -> None:
    page, errors = ui
    page.goto(app_url("schema_explorer", {}), wait_until="domcontentloaded")
    se = find_frame(page, re.compile("Schema Explorer"), 30)
    assert wait_js(se, "document.querySelector('.se-sources')", 20), "no source switch"
    se.get_by_role("tab", name="CAS").click()
    row = se.locator(f'.se-row.db:has(.se-name:text-is("{ss_caslib}"))')
    expect(row).to_have_count(1, timeout=120_000)
    row.click()
    tbl = se.locator('.se-row.tbl:has(.se-name:text-is("CARS"))')
    expect(tbl).to_have_count(1, timeout=120_000)
    tbl.click()
    se.get_by_role("button", name="Load into this session…").click()
    dlg = se.locator(".s2-cl")
    expect(dlg.locator("[data-mode]")).to_have_attribute("data-mode", "live", timeout=60_000)
    expect(dlg).to_contain_text("Live – queries run in SingleStore")
    expect(dlg.locator("[data-size]")).to_contain_text("428 rows")
    assert dlg.get_by_label("WHERE for the load").is_disabled()  # SpeedyStore ignores it
    dlg.get_by_role("button", name="Load into this session").click()
    expect(dlg.locator("[data-loaded]")).to_have_attribute("data-loaded", "live", timeout=120_000)
    dlg.get_by_role("button", name="Close").first.click()
    expect(se.locator('[data-cas-state="live"]')).to_have_count(1, timeout=60_000)
    se.get_by_role("button", name="Unload", exact=True).click()
    expect(se.get_by_role("button", name="Load into this session…")).to_have_count(1, timeout=60_000)
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


@pytest.fixture
def editor(ui, app_url, sas: McpSession):
    page, errors = ui
    sas.call("sas_libraries", {}, timeout=300)  # warm session
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- SAS round UI test"}),
              wait_until="domcontentloaded")
    find_frame(page, "SAS SpeedyStore Studio · Workspace", 30)
    sql = find_frame(page, "SQL Editor", 30)
    assert wait_js(sql, "document.querySelector('.cm-content') && document.querySelector('.sq-tree .sq-row')", 30), text(sql)[-600:]
    tab = sas.ok("sql_editor_tabs")["active"]
    yield page, errors, sql
    sas.call("sql_editor_tab_close", {"tab_id": tab})  # later tests open the active tab and expect SingleStore SQL


@pytest.mark.sas
def test_sql_editor_cas_tree_offers_load(editor, ss_caslib: str) -> None:
    page, errors, sql = editor
    sql.locator('.sq-treesrc button[data-src="cas"]').click()
    lib = sql.locator(f'.sq-tree .sq-row:has(.name:text-is("{ss_caslib}"))').first
    expect(lib).to_have_count(1, timeout=180_000)
    lib.click()
    if sql.get_by_role("button", name="Switch to FedSQL in CAS").count():
        sql.get_by_role("button", name=re.compile("Switch to")).click()
    row = sql.locator('.sq-tree .sq-row.tbl:has(.name:text-is("CARS"))').first
    expect(row).to_have_count(1, timeout=120_000)
    row.hover()
    row.get_by_role("button", name="Load…").click()
    dlg = sql.locator(".s2-cl")
    expect(dlg.locator("[data-mode]")).to_have_attribute("data-mode", "live", timeout=60_000)
    dlg.get_by_role("button", name="Load into this session").click()
    expect(dlg.locator("[data-loaded]")).to_have_attribute("data-loaded", "live", timeout=120_000)
    dlg.get_by_role("button", name="Close").first.click()
    row = sql.locator('.sq-tree .sq-row.tbl:has(.name:text-is("CARS"))').first
    expect(row.locator(".meta")).to_have_text("live", timeout=60_000)
    row.hover()
    row.get_by_role("button", name="Unload").click()
    expect(sql.locator('.sq-tree .sq-row.tbl:has(.name:text-is("CARS"))').first.get_by_role("button", name="Load…")).to_have_count(1, timeout=60_000)
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


@pytest.mark.sas
def test_fedsql_subquery_check_before_the_run(editor, sas: McpSession) -> None:
    page, errors, sql = editor
    auto = sas.ok("sas_libraries", {}, timeout=300).get("auto_libname") or {}
    if not auto.get("assigned"):
        pytest.skip("no automatic SingleStore library")
    lib = auto["libref"]
    sql.get_by_label("Run as", exact=True).select_option("fedsql")
    assert wait_js(sql, f"[...document.querySelectorAll('.sq-tree .sq-row .name')].some((e) => e.textContent === '{lib}')", 180), text(sql)[-500:]
    q = f"select make from {lib}.CARS where msrp > (select avg(msrp) from {lib}.CARS) order by upper(make);"
    sql.evaluate("(d) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: d}))", {"sql": q, "run": True})
    assert wait_js(sql, "document.body.innerText.includes('Check the query')", 20), text(sql)[-500:]
    assert "derived table" in sql.locator(".sq-hint").inner_text()
    sql.get_by_role("button", name="Cancel", exact=True).click()
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs
