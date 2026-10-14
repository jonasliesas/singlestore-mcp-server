"""SAS in the Workspace, in a browser: the SQL Editor's "Run as" selector (PROC SQL with result grid, SAS log
with the generated code, Ctrl+Shift+L, the "looks wrong" check, the language saved with the tab) and the
Schema Explorer's SAS sources. Needs SAS Viya (``sas``); writes nothing outside WORK."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from support import DB, McpSession
from uihelpers import find_frame, text, wait_js

pytestmark = [pytest.mark.ui, pytest.mark.cluster, pytest.mark.sas]


def _set_sql(frame: Any, sql: str, run: bool = True, **extra: Any) -> None:
    frame.evaluate("(d) => window.dispatchEvent(new CustomEvent('s2:set-sql', {detail: d}))", {"sql": sql, "run": run, **extra})


@pytest.fixture
def workspace(ui, app_url):
    page, errors = ui
    page.goto(app_url("sql_editor", {"database": DB, "view": "sql", "new_tab": True, "sql": "-- SAS UI test"}),
              wait_until="domcontentloaded")
    ws = find_frame(page, "SingleStore Workspace", 30)
    sql = find_frame(page, "SQL Editor", 30)
    assert wait_js(sql, "document.querySelector('.cm-content') && document.body.innerText.includes('Ctrl')", 30)
    return page, errors, ws, sql


def test_sql_editor_run_as_proc_sql(workspace, sas: McpSession) -> None:
    page, errors, ws, sql = workspace
    run_as = sql.get_by_label("Run as", exact=True)
    run_as.select_option("procsql")
    assert wait_js(sql, "document.querySelector('.sq-editor .sq-lang')?.textContent === 'PROC SQL'", 5), "editor badge not PROC SQL"
    # The tree lists SAS libraries (the SAS session starts on the first use).
    assert wait_js(sql, "[...document.querySelectorAll('.sq-tree .sq-row .name')].some((e) => e.textContent === 'SASHELP')", 120), \
        "SAS libraries not listed: " + text(sql)[-500:]

    _set_sql(sql, "select name, age, height from sashelp.class where age > 14;")
    assert wait_js(sql, r"/Result \(\d+\)/.test(document.querySelector('.sq-status')?.innerText ?? '')", 90), \
        "no PROC SQL result: " + text(sql)[-600:]
    assert "Philip" in sql.locator(".sq-out").inner_text()
    sql.locator('.sq-sasview button[data-view="log"]').click()
    code = sql.locator(".sq-gencode").inner_text()
    assert "proc sql" in code and "create table WORK._S2RES as" in code, code
    assert sql.locator(".sq-saslog").inner_text().strip(), "empty SAS log"

    # A SAS/ACCESS to SingleStore library: the Pushdown tab shows what SingleStore received and the verdict.
    s2libs = [x for x in sas.ok("sas_libraries")["libraries"] if x["singlestore"]]
    tables = sas.ok("sas_tables", {"library": s2libs[0]["name"]})["tables"] if s2libs else []
    if tables:
        _set_sql(sql, f"select count(*) as n from {s2libs[0]['name']}.{tables[0]['name']};")
        assert wait_js(sql, "document.querySelector('.sq-sasview button[data-view=\"pushdown\"]')", 90), text(sql)[-600:]
        sql.locator('.sq-sasview button[data-view="pushdown"]').click()
        assert wait_js(sql, "/Pushed down fully|Partly pushed down/.test(document.querySelector('.sq-verdict')?.innerText ?? '')", 10), \
            "no pushdown verdict: " + text(sql)[-600:]
        assert "count" in sql.locator(".sq-sent pre").first.inner_text().lower()

    # Plain SAS code in a PROC SQL tab runs unwrapped (badge SAS) and shows its listing.
    _set_sql(sql, "proc means data=sashelp.class n mean; var age; run;")
    assert wait_js(sql, "document.querySelector('.sq-status .sq-lang')?.textContent === 'SAS'", 90), text(sql)[-600:]
    assert wait_js(sql, "document.querySelector('.sq-listing')?.innerText.includes('Mean')", 10), "no listing for PROC MEANS"

    # Ctrl+Shift+L cycles the language; the choice is saved with the tab.
    sql.locator(".cm-content").click()
    page.keyboard.press("Control+Shift+L")
    assert wait_js(sql, "document.querySelector('.sq-runas')?.value === 'fedsql'", 5), "Ctrl+Shift+L didn't switch to FedSQL"
    time.sleep(1.8)  # autosave
    langs = {t["id"]: t.get("language") for t in sas.ok("sql_editor_tabs")["tabs"]}
    assert "fedsql" in langs.values(), langs

    # SingleStore SQL that is really SAS: the editor asks instead of failing.
    run_as.select_option("singlestore")
    _set_sql(sql, "proc print data=sashelp.class; run;")
    assert wait_js(sql, "document.body.innerText.includes('This is SAS code')", 10), "no language warning: " + text(sql)[-400:]
    sql.get_by_role("button", name="Cancel", exact=True).click()
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def test_schema_explorer_sas_sources(ui, app_url, sas: McpSession) -> None:
    page, errors = ui
    page.goto(app_url("schema_explorer", {}), wait_until="domcontentloaded")
    se = find_frame(page, re.compile("Schema Explorer"), 30)
    assert wait_js(se, "document.querySelector('.se-sources')", 20), "no source switch"
    se.get_by_role("tab", name="SAS libraries").click()
    assert wait_js(se, "[...document.querySelectorAll('.se-row.db .se-name')].some((e) => e.textContent === 'SASHELP')", 120), \
        "SAS libraries not listed: " + text(se)[-500:]
    se.locator('.se-row.db:has(.se-name:text-is("SASHELP"))').click()
    assert wait_js(se, "[...document.querySelectorAll('.se-row.tbl .se-name')].some((e) => e.textContent === 'CLASS')", 60), "SASHELP tables"
    se.locator('.se-row.tbl:has(.se-name:text-is("CLASS"))').click()
    assert wait_js(se, "document.querySelector('.se-right')?.innerText.includes('Height')", 60), "CLASS columns: " + text(se)[-400:]
    se.get_by_role("tab", name="Preview").click()
    assert wait_js(se, "document.querySelector('.se-right')?.innerText.includes('Alfred')", 60), "CLASS preview: " + text(se)[-400:]
    # SAS/ACCESS to SingleStore libraries show their database.
    s2libs = se.evaluate("[...document.querySelectorAll('.se-row.db')].filter((e) => e.querySelector('.se-ico.col')).map((e) => e.innerText)")
    if s2libs:
        se.locator(".se-row.db:has(.se-ico.col)").first.click()
        assert wait_js(se, "document.querySelector('.se-right')?.innerText.includes('in SingleStore')", 30), text(se)[-400:]
    se.get_by_role("tab", name="CAS").click()
    assert wait_js(se, "[...document.querySelectorAll('.se-row.db .se-name')].some((e) => /^CASUSER/i.test(e.textContent))", 60), \
        "caslibs not listed: " + text(se)[-500:]
    errs = errors.drain()
    assert not errs, errs
