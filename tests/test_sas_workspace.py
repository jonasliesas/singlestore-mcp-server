"""SAS in the Workspace against SAS Viya (``sas`` marker): libraries, caslibs, Run as PROC SQL / FedSQL / CAS,
the SAS log and the pushdown verdict. Writes only to WORK and CASUSER."""

from __future__ import annotations

import os

import pytest
from support import McpSession

pytestmark = pytest.mark.sas


def _ok(mcp: McpSession, name: str, args: dict | None = None, timeout: float = 300) -> dict:
    r = mcp.call(name, args or {}, timeout=timeout)
    assert not r.is_error, f"{name}: {r.text[:800]}"
    return r.data


# Startup code for the test server's SAS session (its own isolated sas.json): the libname files Jonas keeps in
# SAS Content. Override with S2_TEST_SAS_STARTUP (SAS code), or set it to "" to test without.
STARTUP = os.environ.get("S2_TEST_SAS_STARTUP", "filename _s2inc filesrvc folderpath='/Users/norjni/My Folder/mcp' "
                         "filename='s2_test_libname.sas';\n%include _s2inc / nosource2;\nfilename _s2inc clear;")


@pytest.fixture(scope="module", autouse=True)
def _startup(sas: McpSession):
    _ok(sas, "sas_viya_save", {"settings": {"startup_code": STARTUP}})
    yield


def test_sas_sources_and_libraries(sas: McpSession) -> None:
    st = _ok(sas, "sas_sources")
    assert st["signed_in"]
    libs = _ok(sas, "sas_libraries")["libraries"]
    names = {x["name"] for x in libs}
    assert {"WORK", "SASHELP"} <= names, names
    tables = _ok(sas, "sas_tables", {"library": "SASHELP"})["tables"]
    assert any(t["name"].upper() == "CLASS" for t in tables)
    cols = _ok(sas, "sas_columns", {"library": "SASHELP", "table": "CLASS"})["columns"]
    assert [c["name"].upper() for c in cols][:2] == ["NAME", "SEX"]
    assert {c["type"] for c in cols} == {"char", "num"}


def test_proc_sql_select_rows_and_log(sas: McpSession) -> None:
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "select name, age, height from sashelp.class where age > 13;",
                               "library": "SASHELP", "max_rows": 3, "pushdown": True})
    assert d["has_result_set"] and d["row_count"] == 3 and d["truncated"]
    assert [c.upper() for c in d["columns"]] == ["NAME", "AGE", "HEIGHT"]
    assert "create table WORK._S2RES as" in d["code"] and "proc sql outobs=3" in d["code"]
    assert "proc sql" in d["log"].lower() and not d["error"]
    assert d["pushdown"]["verdict"] == "none"


def test_proc_sql_error_is_reported_with_log(sas: McpSession) -> None:
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "select nosuchcolumn from sashelp.class"})
    assert d["error"] and "ERROR" in d["log"] and d["errors"] >= 1 and not d["has_result_set"]


def test_proc_sql_write_to_work_runs_and_elsewhere_asks(sas: McpSession) -> None:
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "create table work.s2ws_test as select * from sashelp.class"})
    assert not d.get("needs_confirmation") and not d["error"] and d["affected_rows"] == 19
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "create table sasuser.s2ws_test as select * from sashelp.class"})
    assert d["needs_confirmation"] and d["libraries"] == ["SASUSER"]


def test_plain_sas_runs_unwrapped(sas: McpSession) -> None:
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "proc means data=sashelp.class n mean; var age; run;"})
    assert d["plain_sas"] and d["code"].startswith("proc means") or sas_trace_first(d["code"])
    assert d["listing"] and "Mean" in d["listing"]


def sas_trace_first(code: str) -> bool:
    return code.splitlines()[1].startswith("proc means")


def test_fedsql_select(sas: McpSession) -> None:
    d = _ok(sas, "sas_query", {"language": "fedsql", "sql": 'select "Name", "Age" from work."S2WS_TEST" where "Age" > 14',
                               "max_rows": 100})
    if d.get("error"):
        pytest.fail(d["error"] + "\n" + d["log"][-2000:])
    assert d["has_result_set"] and d["row_count"] >= 1


def test_cas_caslibs_and_fedsql(sas: McpSession) -> None:
    libs = _ok(sas, "cas_caslibs")["caslibs"]
    assert any(x["name"].upper().startswith("CASUSER") for x in libs)
    d = _ok(sas, "sas_query", {"language": "cas", "sql": "select 1 as one, 'a' as letter from (select 1 as x) t",
                               "library": "CASUSER", "max_rows": 10})
    if d.get("error"):
        pytest.fail(d["error"] + "\n" + d["log"][-2000:])
    assert d["has_result_set"] and d["row_count"] == 1


def test_singlestore_library_pushdown(sas: McpSession) -> None:
    libs = _ok(sas, "sas_libraries")["libraries"]
    s2 = [x for x in libs if x["singlestore"]]
    if not s2:
        pytest.skip("No SAS/ACCESS to SingleStore library in the session (startup code not set?)")
    lib = s2[0]
    assert lib["database"]
    tables = _ok(sas, "sas_tables", {"library": lib["name"]})["tables"]
    if not tables:
        pytest.skip(f"{lib['name']} has no tables")
    t = tables[0]["name"]
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": f"select count(*) as n from {lib['name']}.{t}", "max_rows": 10})
    assert not d["error"], d["log"][-2000:]
    pd = d["pushdown"]
    assert pd["verdict"] in ("full", "partly"), pd
    assert pd["sent"] and "count" in pd["sent"][0]["sql"].lower()
    # The statement SingleStore received is in its plan cache now.
    found = _ok(sas, "sas_history_lookup", {"sql": pd["sent"][0]["sql"], "database": lib["database"]})
    assert found["found"] and found["plan_cache"], found


def test_history_records_language(sas: McpSession) -> None:
    _ok(sas, "sas_query", {"language": "procsql", "sql": "select count(*) as n from sashelp.class"})
    h = _ok(sas, "sql_editor_history", {"limit": 5})
    assert any(e.get("language") == "procsql" for e in h["entries"])
