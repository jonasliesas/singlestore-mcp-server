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


@pytest.fixture(scope="module")
def sas_warm(sas: McpSession) -> McpSession:
    """Start the test server's SAS session before the browser needs it: a cold session takes ~35 s plus the startup
    code, longer under load, and the UI's waits should be about the UI. One retry for a dropped connection."""
    for attempt in range(2):
        r = sas.call("sas_libraries", {}, timeout=300)
        if not r.is_error:
            return sas
        if attempt:
            pytest.fail(f"SAS session didn't start: {r.text[:400]}")
    return sas


@pytest.fixture
def workspace(ui, app_url, sas_warm):
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
    # The tree lists SAS libraries (the session is warm: sas_warm); an error in the tree ends the wait at once.
    listed = wait_js(sql, "[...document.querySelectorAll('.sq-tree .sq-row .name')].some((e) => e.textContent === 'SASHELP') ? 'ok'"
                          " : (document.querySelector('.sq-tree .s2-error')?.innerText ?? '')", 180)
    assert listed == "ok", "SAS libraries not listed: " + (listed or text(sql)[-500:])

    _set_sql(sql, "select name, age, height from sashelp.class where age > 14;")
    assert wait_js(sql, r"/Result \(\d+\)/.test(document.querySelector('.sq-status')?.innerText ?? '')", 90), \
        "no PROC SQL result: " + text(sql)[-600:]
    assert "Philip" in sql.locator(".sq-out").inner_text()
    sql.locator('.sq-sasview button[data-view="log"]').click()
    code = sql.locator(".sq-gencode").inner_text()
    assert "proc sql" in code and re.search(r"create table WORK\._S2R\w* as", code), code  # the tab's own result table
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
    deadline = time.monotonic() + 20  # autosave (~1.5 s after the change; slower under load)
    while True:
        langs = {t["id"]: t.get("language") for t in sas.ok("sql_editor_tabs")["tabs"]}
        if "fedsql" in langs.values() or time.monotonic() > deadline:
            break
        time.sleep(0.3)
    assert "fedsql" in langs.values(), langs

    # SingleStore SQL that is really SAS: the editor asks instead of failing.
    run_as.select_option("singlestore")
    _set_sql(sql, "proc print data=sashelp.class; run;")
    assert wait_js(sql, "document.body.innerText.includes('This is SAS code')", 10), "no language warning: " + text(sql)[-400:]
    sql.get_by_role("button", name="Cancel", exact=True).click()
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def test_sql_editor_sas_autocomplete_fetches_columns(workspace, sas: McpSession) -> None:
    """Typing "select c. from sashelp.class c" in a PROC SQL tab suggests CLASS's columns after "c." without the
    table being expanded in the tree: the editor looks them up in the background (alias resolved)."""
    page, errors, ws, sql = workspace
    sql.get_by_label("Run as", exact=True).select_option("procsql")
    assert wait_js(sql, "document.querySelector('.sq-editor .sq-lang')?.textContent === 'PROC SQL'", 5)
    sql.locator(".cm-content").click()
    page.keyboard.press("Control+A")
    page.keyboard.type("select c. from sashelp.class c")
    page.keyboard.press("Escape")
    page.keyboard.press("Home")
    for _ in range(len("select c.")):
        page.keyboard.press("ArrowRight")
    page.keyboard.press("Control+Space")
    # The first SAS use starts the session (~35 s cold): the suggestions open when the columns arrive.
    got = wait_js(sql, "[...document.querySelectorAll('.cm-tooltip-autocomplete li .cm-completionLabel')].map((e) => e.textContent)"
                       ".filter((t) => /^(Name|Sex|Age|Height|Weight)$/i.test(t))", 150)
    assert got and len(got) == 5, "no CLASS columns suggested after c.: " + text(sql)[-500:]
    assert "Height" not in sql.locator(".sq-tree").inner_text(), "CLASS was expanded in the tree"
    # Click the option (not Enter: the popup can be reopened in between – e.g. when SASHELP's table list arrives –
    # and Enter on a closed popup inserts a line break). Playwright re-finds the option if it's re-rendered.
    sql.locator(".cm-tooltip-autocomplete li", has=sql.locator(".cm-completionLabel", has_text="Age")).first.click()
    assert wait_js(sql, r"/^select c\.age from sashelp\.class c$/i.test(document.querySelector('.cm-content').innerText.trim())", 10), \
        sql.locator(".cm-content").inner_text()
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


def test_schema_explorer_singlestore_badges_and_show_code(ui, app_url, sas: McpSession) -> None:
    """CAS: SpeedyStore caslibs are badged SS, standard-connector ones S2 (outlined); Show code rebuilds the CASLIB
    statement / table.addCaslib with the password as a placeholder, and a SAS library's LIBNAME."""
    page, errors = ui
    page.goto(app_url("schema_explorer", {}), wait_until="domcontentloaded")
    se = find_frame(page, re.compile("Schema Explorer"), 30)
    assert wait_js(se, "document.querySelector('.se-sources')", 20), "no source switch"
    se.get_by_role("tab", name="CAS").click()
    assert wait_js(se, "document.querySelectorAll('.se-row.db').length > 1", 60), "caslibs not listed: " + text(se)[-500:]
    libs = {x["name"]: x for x in sas.ok("cas_caslibs")["caslibs"]}
    ss = [n for n, x in libs.items() if x.get("singlestore_kind") == "speedystore"]
    std = [n for n, x in libs.items() if x.get("singlestore_kind") == "standard"]
    if ss:
        assert se.locator(f'.se-row.db:has(.se-name:text-is("{ss[0]}")) .se-ico.ss').inner_text() == "SS"
        assert "SpeedyStore" in se.locator(f'.se-row.db:has(.se-name:text-is("{ss[0]}")) .se-ico').get_attribute("title")
    if std:
        assert se.locator(f'.se-row.db:has(.se-name:text-is("{std[0]}")) .se-ico.s2-std').inner_text() == "S2"
    pick = (ss or std or [next(n for n, x in libs.items() if x.get("personal"))])[0]
    se.locator(f'.se-row.db:has(.se-name:text-is("{pick}"))').click()
    se.get_by_role("button", name="Show code").click()
    assert wait_js(se, "document.querySelector('.se-code')?.innerText.includes('table.addCaslib') ? 'ok' : "
                       "(document.querySelector('.se-code .s2-error')?.innerText ?? '')", 60) == "ok", text(se)[-600:]
    code = se.locator(".se-code").inner_text()
    assert "caslib" in code.lower() and "Reconstructed" in code
    if libs[pick].get("secret_options"):
        assert "<set password>" in code or "<authentication domain>" in code
    assert se.get_by_role("button", name="Open in SQL Editor").count() == 2  # statement and action
    se.get_by_role("tab", name="SAS libraries").click()
    assert wait_js(se, "[...document.querySelectorAll('.se-row.db .se-name')].some((e) => e.textContent === 'WORK')", 120)
    se.locator('.se-row.db:has(.se-name:text-is("WORK"))').click()
    se.get_by_role("button", name="Show code").click()
    assert wait_js(se, "document.querySelector('.se-code pre')?.innerText.includes('libname WORK')", 60), text(se)[-400:]
    errs = errors.drain()
    assert not errs, errs


def _clear_editor(page: Any, sql: Any) -> None:
    sql.locator(".cm-content").click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")


def test_sql_editor_stop_a_long_sas_run(workspace) -> None:
    """A long SAS run shows its elapsed time and a Stop button; Stop cancels the job and the result says "stopped"
    with the SAS log so far (the session keeps running: the next run works)."""
    page, errors, ws, sql = workspace
    sql.get_by_label("Run as", exact=True).select_option("procsql")
    _set_sql(sql, "data _null_; call sleep(120, 1); run;")
    assert wait_js(sql, "document.querySelector('.sq-status')?.innerText.includes('Running in SAS')", 120), text(sql)[-500:]
    sql.locator(".sq-status").get_by_role("button", name="Stop", exact=True).click()
    assert wait_js(sql, "[...document.querySelectorAll('.sq-status .s2-badge')].some((e) => e.textContent === 'stopped')", 60), \
        "not stopped: " + text(sql)[-600:]
    assert "call sleep" in sql.locator(".sq-saslog").inner_text().lower(), "no log after Stop"
    _set_sql(sql, "select name from sashelp.class where age > 15;")
    assert wait_js(sql, r"/Result \(\d+\)/.test(document.querySelector('.sq-status')?.innerText ?? '')", 90), text(sql)[-600:]
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def test_sql_editor_sas_result_pages_as_you_scroll(workspace) -> None:
    page, errors, ws, sql = workspace
    sql.get_by_label("Run as", exact=True).select_option("procsql")
    _set_sql(sql, "select a.make, b.name from sashelp.cars a, sashelp.class b;")  # 428 x 19 = 8,132 rows
    assert wait_js(sql, "document.querySelector('.sq-status')?.innerText.includes('8,132 rows')", 120), text(sql)[-600:]
    assert "1,000 shown" in sql.locator(".sq-status").inner_text()
    for _ in range(6):
        sql.evaluate("document.querySelector('.sq-out').scrollTop = 1e9")
        if wait_js(sql, "!document.querySelector('.sq-status')?.innerText.includes('1,000 shown')", 5):
            break
    shown = sql.locator(".sq-status").inner_text()
    assert "1,000 shown" not in shown, shown
    assert sql.locator(".sq-out tbody tr").count() > 1000
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def test_tree_click_switches_the_language(workspace) -> None:
    """The tree can list SingleStore, SAS libraries or caslibs. Using a SAS library in an empty SingleStore tab
    switches it to PROC SQL at once; with SQL in the tab it asks first (and Keep leaves it as it was)."""
    page, errors, ws, sql = workspace
    run_as = sql.get_by_label("Run as", exact=True)
    run_as.select_option("singlestore")
    _clear_editor(page, sql)
    sql.locator('.sq-treesrc button[data-src="sas"]').click()
    assert wait_js(sql, "[...document.querySelectorAll('.sq-tree .sq-row .name')].some((e) => e.textContent === 'SASHELP')", 180), text(sql)[-500:]
    sql.locator('.sq-tree .sq-row:has(.name:text-is("SASHELP"))').first.click()
    assert wait_js(sql, "document.querySelector('.sq-runas')?.value === 'procsql'", 10), "empty tab didn't switch to PROC SQL"
    # With text in the tab: a SingleStore database asks first.
    _set_sql(sql, "select 1 as x from sashelp.class;", run=False)
    sql.locator('.sq-treesrc button[data-src="s2"]').click()
    assert wait_js(sql, f"[...document.querySelectorAll('.sq-tree .sq-row .name')].some((e) => e.textContent === '{DB}')", 30)
    sql.locator(f'.sq-tree .sq-row:has(.name:text-is("{DB}"))').first.click()
    assert wait_js(sql, "document.body.innerText.includes('Switch to SingleStore SQL')", 10), text(sql)[-500:]
    sql.get_by_role("button", name="Keep PROC SQL").click()
    assert run_as.input_value() == "procsql"
    sql.locator(f'.sq-tree .sq-row:has(.name:text-is("{DB}"))').first.click()
    sql.get_by_role("button", name="Switch to SingleStore SQL").click()
    assert wait_js(sql, "document.querySelector('.sq-runas')?.value === 'singlestore'", 10)
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def test_tab_pins_its_viya_connection(workspace, sas: McpSession) -> None:
    """The ⚙ panel picks the tab's SAS Viya connection (default: the active one); it's saved with the tab and the
    tab's SAS runs use it."""
    page, errors, ws, sql = workspace
    sql.get_by_label("Run as", exact=True).select_option("procsql")
    sql.get_by_role("button", name="SAS options").click()
    pick = sql.locator(".sq-sasopts select.sq-viya")
    assert wait_js(sql, "document.querySelector('.sq-sasopts select.sq-viya')", 30), text(sql)[-400:]
    active = sas.ok("sas_connections_state")["active"]
    pick.select_option(active)
    assert wait_js(sql, "document.querySelector('.sq-sasopts select.sq-viya')?.classList.contains('pinned')", 5)
    _set_sql(sql, "select count(*) as n from sashelp.class;")
    assert wait_js(sql, r"/Result \(\d+\)/.test(document.querySelector('.sq-status')?.innerText ?? '')", 90), text(sql)[-600:]
    assert f"Viya: {active}" in sql.locator(".sq-status").inner_text()
    deadline = time.monotonic() + 20
    pinned = None
    while time.monotonic() < deadline and pinned != active:
        tab_id = sas.ok("sql_editor_tabs")["active"]
        pinned = ((sas.ok("sql_editor_state", {"tab_id": tab_id})["draft"] or {}).get("sas") or {}).get("connection")
        time.sleep(0.5)
    assert pinned == active
    pick.select_option("")  # back to the active one
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs


def test_fedsql_pushdown_find_in_query_history(workspace, sas: McpSession) -> None:
    """FedSQL through the automatic SingleStore library: the Pushdown tab shows the SQL SingleStore received (driver
    trace) and "Find in Query History" opens Query History on it, also when only the plan cache knows it."""
    page, errors, ws, sql = workspace
    st = sas.ok("sas_libraries")
    auto = st.get("auto_libname") or {}
    if not auto.get("assigned"):
        pytest.skip(f"No automatic SingleStore library: {auto.get('note') or auto.get('error')}")
    tables = sas.ok("sas_tables", {"library": auto["libref"]})["tables"]
    if not tables:
        pytest.skip("the automatic library has no tables")
    sql.get_by_label("Run as", exact=True).select_option("fedsql")
    _set_sql(sql, f'select count(*) as n from {auto["libref"]}."{tables[0]["name"]}" where 2 = 2;')
    assert wait_js(sql, "document.querySelector('.sq-sasview button[data-view=\"pushdown\"]')", 120), text(sql)[-600:]
    sql.locator('.sq-sasview button[data-view="pushdown"]').click()
    assert "count" in sql.locator(".sq-sent pre").first.inner_text().lower()
    sql.locator(".sq-sent").first.get_by_role("button", name="Find in Query History").click()
    qh = find_frame(page, re.compile("Query History"), 60)
    # Only in the plan cache (a run under 1 s): Query History lists and selects its plan-cache entry; else its search.
    assert wait_js(qh, "document.querySelector('.qh-detail')?.innerText.toLowerCase().includes('count')"
                       " || [...document.querySelectorAll('.qh-table td.sql')].some((e) => e.textContent.toLowerCase().includes('count'))", 60),         text(qh)[-600:]
    errs = [e for e in errors.drain() if "favicon" not in e]
    assert not errs, errs
