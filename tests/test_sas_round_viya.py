"""Against SAS Viya (``--sas``): "Load into this session" on a SpeedyStore caslib (live) and a standard SingleStore
caslib (copied into CAS memory), FedSQL in CAS on the loaded table, Unload, reload after Restart SAS session; a
FedSQL SELECT with a subquery on the automatic S2 library (runs as a derived table); notebook SAS cells sharing WORK
and macro variables with the SQL Editor and Python in SAS cells.

Read-only on Zeus apart from session-scope loads of the small CARS table (428 rows, ~100 KB; dropped afterwards)
and WORK tables. The caslibs: S2_TEST_SS_CASLIB (default CSASDP) and S2_TEST_STD_CASLIB (default SASDPSTD)."""

from __future__ import annotations

import os
import time

import pytest
from support import McpSession

pytestmark = pytest.mark.sas

SS = os.environ.get("S2_TEST_SS_CASLIB", "CSASDP")
STD = os.environ.get("S2_TEST_STD_CASLIB", "SASDPSTD")
TABLE = "CARS"


def _caslib(sas: McpSession, name: str, kind: str) -> dict:
    libs = {x["name"].upper(): x for x in sas.ok("cas_caslibs", timeout=300)["caslibs"]}
    lib = libs.get(name.upper())
    if not lib or lib.get("singlestore_kind") != kind:
        pytest.skip(f"no {kind} caslib {name} on this CAS server")
    return lib


def _cas_count(sas: McpSession, caslib: str) -> int:
    q = sas.ok("sas_query", {"language": "cas", "sql": f"select count(*) as n from {caslib}.{TABLE}", "library": caslib,
                             "history": False, "pushdown": False}, timeout=300)
    assert not q["error"], q["error"]
    return int(q["rows"][0][q["columns"][0]])


def _drop(sas: McpSession, caslib: str) -> None:
    sas.call("cas_unload_table", {"caslib": caslib, "table": TABLE}, timeout=120)


def test_speedystore_load_is_live_and_unloads(sas: McpSession) -> None:
    _caslib(sas, SS, "speedystore")
    _drop(sas, SS)
    try:
        info = sas.ok("cas_load_info", {"caslib": SS, "table": TABLE}, timeout=300)
        assert info["mode"] == "live" and info["bytes"] and info["source"]["rows"] == 428 and not info["confirm"]
        d = sas.ok("cas_load_table", {"caslib": SS, "table": TABLE, "where": "Origin = 'Asia'"}, timeout=300)
        assert d["live"] and d["location"] == "SINGLESTORE" and not d["memory"] and d["where_ignored"], d
        assert _cas_count(sas, SS) == 428  # SpeedyStore ignored the WHERE on the load (as checked by hand)
        tables = {t["name"].upper(): t for t in sas.ok("cas_tables", {"caslib": SS}, timeout=300)["tables"]}
        assert tables[TABLE]["loaded"] and tables[TABLE]["session"] and tables[TABLE]["live"]
    finally:
        u = sas.ok("cas_unload_table", {"caslib": SS, "table": TABLE}, timeout=120)
    assert u["dropped"]
    # The source table is still there (only the session table went).
    assert sas.ok("cas_load_info", {"caslib": SS, "table": TABLE}, timeout=300)["source"]["rows"] == 428


def test_standard_caslib_copies_into_cas_memory(sas: McpSession) -> None:
    _caslib(sas, STD, "standard")
    _drop(sas, STD)
    try:
        d = sas.ok("cas_load_table", {"caslib": STD, "table": TABLE, "where": "Origin = 'Asia'", "columns": "Make, Model, Origin"},
                   timeout=300)
        assert d["needs_confirmation"] and "can't tell" in d["reason"]  # the standard connector doesn't say the size
        d = sas.ok("cas_load_table", {"caslib": STD, "table": TABLE, "where": "Origin = 'Asia'", "columns": "Make, Model, Origin",
                                      "confirmed": True}, timeout=300)
        assert not d["live"] and d["location"] == "CAS" and d["memory"] > 0 and d["column_count"] == 3, d
        assert _cas_count(sas, STD) == 158
    finally:
        _drop(sas, STD)


def test_reload_after_restart_sas_session(sas: McpSession) -> None:
    _caslib(sas, SS, "speedystore")
    _drop(sas, SS)
    try:
        sas.ok("cas_load_table", {"caslib": SS, "table": TABLE, "columns": "Make, MSRP"}, timeout=300)
        sas.ok("sas_session_reset", {}, timeout=120)
        st = sas.ok("cas_session_loads", {}, timeout=60)
        assert [x["table"].upper() for x in st["lost"]] == [TABLE] and not st["loaded"]
        assert any(x["table"].upper() == TABLE for x in sas.ok("cas_caslibs", {}, timeout=300)["session_loads"]["lost"])
        r = sas.ok("cas_reload_tables", {}, timeout=300)
        assert [x["table"].upper() for x in r["reloaded"]] == [TABLE] and not r["failed"]
        assert _cas_count(sas, SS) == 428
    finally:
        _drop(sas, SS)


def test_fedsql_subquery_on_the_s2_library_runs_as_a_derived_table(sas: McpSession) -> None:
    auto = sas.ok("sas_libraries", {}, timeout=300).get("auto_libname") or {}
    if not auto.get("assigned"):
        pytest.skip(f"No automatic SingleStore library: {auto.get('note') or auto.get('error')}")
    names = {t["name"].upper() for t in sas.ok("sas_tables", {"library": auto["libref"]}, timeout=300)["tables"]}
    if TABLE not in names:
        pytest.skip(f"no {TABLE} in {auto['libref']}")
    lib = auto["libref"]
    sql = f"select make, model, msrp from {lib}.CARS where msrp > (select avg(msrp) from {lib}.CARS) order by msrp desc"
    d = sas.ok("sas_query", {"language": "fedsql", "sql": sql, "history": False, "pushdown": True}, timeout=300)
    assert not d["error"], d["error"]
    assert d["derived"] and "_s2q" in d["code"] and d["row_count"] > 0
    msrp = [r.get("MSRP", r.get("msrp")) for r in d["rows"]]
    assert msrp == sorted(msrp, reverse=True)  # the ORDER BY repeated outside the derived table
    assert d["pushdown"]["verdict"] in ("full", "partly") and any("_s2q" in s["sql"].lower() for s in d["pushdown"]["sent"])
    # An ORDER BY on an expression can't be repeated: the pre-run check says so (nothing runs).
    chk = sas.ok("sas_fedsql_check", {"sql": sql.replace("order by msrp desc", "order by upper(make)")})
    assert chk["warn"] and "derived table" in chk["message"]


def _cell(sas: McpSession, tool: str, source: str, run_id: str, timeout: float = 300) -> dict:
    r = sas.call(tool, {"source": source, "notebook_id": "zz-pytest-sascell", "run_id": run_id, "wait_ms": 2000}, timeout=timeout)
    assert not r.is_error, r.text[:800]
    d = r.data
    t0 = time.perf_counter()
    while d.get("running"):
        assert time.perf_counter() - t0 < timeout, f"{run_id} still running"
        d = sas.ok("notebook_sas_python_status", {"run_id": run_id, "wait_ms": 10_000}, timeout=60)
    return d


def _sas_out(d: dict) -> dict:
    return next(o["data"]["application/vnd.s2.sas+json"] for o in d["outputs"] if o["output_type"] == "display_data")


def test_sas_cells_share_work_and_macro_variables(sas: McpSession) -> None:
    d = _cell(sas, "notebook_sas_cell", "data work.zz_sascell; set sashelp.class; where age > 14; run;\n%let zz_sascell_n = 5;",
              "pytest-sc-a")
    assert d["status"] == "ok" and _sas_out(d)["errors"] == 0, d
    assert "WORK.ZZ_SASCELL has 5 observations" in _sas_out(d)["log"]
    # The SQL Editor's SAS session has the table (same Compute session, same WORK) …
    q = sas.ok("sas_query", {"language": "procsql", "sql": "select count(*) as n from work.zz_sascell", "history": False}, timeout=300)
    assert q["rows"][0][q["columns"][0]] == 5
    # … and a Python in SAS cell the macro variable and the table.
    p = _cell(sas, "notebook_sas_python", 'print(SAS.symget("zz_sascell_n"), len(SAS.sd2df("work.zz_sascell")))', "pytest-sc-b")
    out = "".join(o["text"] for o in p["outputs"] if o["output_type"] == "stream" and o["name"] == "stdout")
    assert out.strip().endswith("5 5"), p
    # ODS output comes back as HTML; an error is counted and named.
    d = _cell(sas, "notebook_sas_cell", "proc print data=work.zz_sascell; run;", "pytest-sc-c")
    assert d["status"] == "ok" and "<table" in (_sas_out(d)["listing"] or "")
    d = _cell(sas, "notebook_sas_cell", "data _null_; set work.zz_no_such_table; run;", "pytest-sc-d")
    assert d["status"] == "error" and _sas_out(d)["errors"] == 1 and "does not exist" in _sas_out(d)["first_error"]
    # The next cell runs normally (the session reset clears syntax check mode).
    d = _cell(sas, "notebook_sas_cell", "proc means data=work.zz_sascell n; var age; run;", "pytest-sc-e")
    assert d["status"] == "ok", d
