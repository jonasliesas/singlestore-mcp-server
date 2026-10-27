"""SAS in the Workspace against SAS Viya (``sas`` marker): libraries, caslibs, Run as PROC SQL / FedSQL / CAS,
the SAS log and the pushdown verdict. Writes only to WORK and CASUSER."""

from __future__ import annotations

import os
import time

import pytest
from support import McpSession

from singlestore_mcp import sas_sql

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
    # The session reset (sas_sql.SESSION_RESET) comes first, then the trace options when pushdown is on.
    lines = [x for x in code.splitlines() if x not in (sas_sql.SESSION_RESET, sas_sql.TRACE_ON)]
    return lines[0].startswith("proc means")


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


def test_lookup_columns_for_autocomplete(sas: McpSession) -> None:
    """The SQL Editor's background column lookup: several tables in one call, a missing table is ``missing``
    (no error, and the SAS session isn't restarted for it), a lower-case name finds the table."""
    d = _ok(sas, "sas_lookup_columns", {"language": "procsql", "tables": [
        {"library": "sashelp", "table": "class"}, {"library": "SASHELP", "table": "S2_NO_SUCH_TABLE"},
        {"library": "NOSUCHLB", "table": "X"}]})
    by = {(t["library"], t["table"].upper()): t for t in d["tables"]}
    cols = by[("SASHELP", "CLASS")]["columns"]
    assert [c["name"].upper() for c in cols][:2] == ["NAME", "SEX"]
    assert by[("SASHELP", "S2_NO_SUCH_TABLE")]["missing"] and by[("NOSUCHLB", "X")]["missing"]
    again = _ok(sas, "sas_lookup_columns", {"language": "procsql", "tables": [{"library": "SASHELP", "table": "CARS"}]})
    assert again["session"] == d["session"], "a missing table restarted the SAS session"
    assert not again["tables"][0]["missing"]
    c = _ok(sas, "sas_lookup_columns", {"language": "cas", "tables": [{"library": "CASUSER", "table": "S2_NO_SUCH_TABLE"}]})
    assert c["tables"][0]["missing"] and c["session"].split("|")[1] == "cas"


# ------------------------------------------------------------------ automatic S2 library, Show code, SAS 9


def _s2_password() -> str | None:
    """The active SingleStore connection's password (the test server shares the OS credential store)."""
    from singlestore_mcp import connections

    p = connections.active()
    if p is None or p.url or p.auth != "password":
        return None
    return connections.password_for(p) or None


def test_automatic_singlestore_library_works_and_hides_its_password(sas: McpSession, test_home, monkeypatch: pytest.MonkeyPatch) -> None:
    st = _ok(sas, "sas_libraries", {"refresh": True})
    auto = st.get("auto_libname") or {}
    if not auto.get("assigned"):
        pytest.skip(f"No automatic SingleStore library: {auto.get('note') or auto.get('error')}")
    lib = next(x for x in st["libraries"] if x["name"] == auto["libref"])
    assert lib["singlestore"] and lib["badge"] == "S2" and lib["database"] == auto["database"]
    tables = _ok(sas, "sas_tables", {"library": lib["name"]})["tables"]
    if not tables:
        pytest.skip(f"{lib['name']} ({lib['database']}) has no tables")
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": f"select count(*) as n from {lib['name']}.{tables[0]['name']}"})
    assert not d["error"], d["log"][-2000:]
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(test_home))
    pw = _s2_password()
    if pw:
        for text in (d["log"], d["code"], repr(st), repr(_ok(sas, "sas_sources")), repr(_ok(sas, "sql_editor_history", {"limit": 20}))):
            assert pw not in text
        code = _ok(sas, "sas_library_code", {"library": lib["name"]})["code"]
        assert pw not in code and 'password="<set password>"' in code and "port=" in code


def test_show_code_for_libraries_and_caslibs(sas: McpSession) -> None:
    code = _ok(sas, "sas_library_code", {"library": "WORK"})
    assert "libname WORK" in code["code"] and "Reconstructed" in code["code"]
    libs = _ok(sas, "cas_caslibs")["caslibs"]
    by_kind = {x.get("singlestore_kind"): x for x in libs if x.get("singlestore_kind") and x.get("database")}
    for kind, badge in (("speedystore", "SS"), ("standard", "S2")):
        if kind not in by_kind:
            continue
        lib = by_kind[kind]
        assert lib["badge"] == badge and lib["database"] in lib["badge_title"]
        c = _ok(sas, "cas_caslib_code", {"caslib": lib["name"]})
        assert f'srctype="{lib["type"].lower()}"' in c["statement"] and "table.addCaslib" in c["action"]
        if lib.get("secret_options"):
            assert "<set password>" in c["statement"] or "<authentication domain>" in c["statement"]
    personal = next(x for x in libs if x.get("personal"))
    c = _ok(sas, "cas_caslib_code", {"caslib": personal["name"]})
    assert "personal caslib" in c["statement"]


SAS9_SIGNON = os.environ.get("S2_TEST_SAS9_SIGNON", "filename _s2s9 filesrvc folderpath='/Users/norjni/My Folder/mcp' "
                             "filename='cauki_signon.sas';\n%include _s2s9 / nosource2;\nfilename _s2s9 clear;")


def test_sas9_libraries_through_sas_connect(sas: McpSession) -> None:
    """SAS 9 (CAUKI) through SAS/CONNECT. CAUKI is shut down every evening at 22:00: no answer then is skipped."""
    _ok(sas, "sas_viya_save", {"settings": {"sas9_signon": SAS9_SIGNON, "sas9_server": os.environ.get("S2_TEST_SAS9_SERVER", "CAUKI"),
                                            "sas9_libraries": os.environ.get("S2_TEST_SAS9_LIBRARIES", "STG SASDATA")}})
    try:
        r = sas.call("sas9_libraries", {}, timeout=600)
        if r.is_error:
            pytest.skip(f"SAS 9 sign-on failed (server down for the night?): {r.text[:300]}")
        libs = r.data["libraries"]
        assert libs and all(x["sas9"] and x["badge"] == "9" for x in libs)
        lib = libs[0]
        code = _ok(sas, "sas_library_code", {"library": lib["name"]})["code"]
        assert f"server={r.data['server']} slibref=" in code
        tables = _ok(sas, "sas_tables", {"library": lib["name"]})["tables"]
        if tables:
            d = _ok(sas, "sas_query", {"language": "procsql", "sql": f"select count(*) as n from {lib['name']}.{tables[0]['name']}"})
            assert not d["error"], d["log"][-2000:]
        assert any(x["name"] == lib["name"] for x in _ok(sas, "sas_libraries")["libraries"])
    finally:
        _ok(sas, "sas_viya_save", {"settings": {"sas9_signon": "", "sas9_server": "", "sas9_libraries": ""}})


# ------------------------------------------------------------------ Stop, queue, paging, per-tab connection, FedSQL trace

LONG = "data _null_; call sleep(120, 1); run;"  # two minutes of nothing (WORK only)


def _status(mcp: McpSession, run_id: str, wait_ms: int = 20000) -> dict:
    return _ok(mcp, "sas_run_status", {"run_id": run_id, "wait_ms": wait_ms}, timeout=120)


def test_stop_a_long_run_keeps_the_session(sas: McpSession) -> None:
    """Stop cancels the SAS job (not the session): the run ends "stopped" within seconds with the log so far, and
    the next run in the same session works."""
    _ok(sas, "sas_query", {"language": "procsql", "sql": "select 1 as x from sashelp.class"})  # a warm session (a Stop
    # while the session starts ends the run before it starts, with no log)
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": LONG, "run_id": "s_zz_stop_1", "wait_ms": 4000, "tab": "stop test"})
    assert d["running"], d
    started = time.monotonic()
    st = _ok(sas, "sas_stop", {"run_id": "s_zz_stop_1"})
    assert st["stopped"] and st.get("cancel_sent") is not False, st
    d = _status(sas, "s_zz_stop_1")
    assert not d["running"] and d["stopped"] and d["run_state"] == "stopped", d
    assert time.monotonic() - started < 60, "Stop took as long as the job"
    assert not d["error"] and "call sleep" in d["log"].lower(), (d.get("note"), d["log"][-1000:])
    assert _ok(sas, "sas_sources")["compute_session"], "Stop ended the SAS session"
    nxt = _ok(sas, "sas_query", {"language": "procsql", "sql": "select count(*) as n from sashelp.class"})
    assert nxt["has_result_set"] and nxt["rows"][0][nxt["columns"][0]] == 19


def test_second_tab_waits_in_line_and_says_for_which(sas: McpSession) -> None:
    _ok(sas, "sas_query", {"language": "procsql", "sql": LONG, "run_id": "s_zz_queue_a", "wait_ms": 3000, "tab": "Tab A", "tab_id": "zzA"})
    b = _ok(sas, "sas_query", {"language": "procsql", "sql": "select count(*) as n from sashelp.class", "run_id": "s_zz_queue_b",
                               "wait_ms": 3000, "tab": "Tab B", "tab_id": "zzB"})
    assert b["running"] and b["state"] == "queued" and b["waiting_for"]["tab"] == "Tab A", b
    _ok(sas, "sas_stop", {"run_id": "s_zz_queue_a"})
    assert _status(sas, "s_zz_queue_a")["stopped"]
    b = _status(sas, "s_zz_queue_b", 25000)
    if b.get("running"):
        b = _status(sas, "s_zz_queue_b", 25000)
    assert not b["running"] and b["has_result_set"] and not b["error"], b


def test_paging_compute_and_cas_results(sas: McpSession) -> None:
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "select make, model from sashelp.cars", "max_rows": 100,
                               "table_rows": 100000, "tab_id": "zzpage", "run_id": "s_zz_page_1"})
    assert d["row_count"] == 100 and d["total_rows"] == 428 and d["truncated"], {k: d[k] for k in ("row_count", "total_rows")}
    p = _ok(sas, "sas_rows", {"run_id": "s_zz_page_1", "offset": 100, "limit": 400})
    assert len(p["rows"]) == 328 and not p["more"]
    assert p["rows"][0] != d["rows"][0]
    # CAS: a CASUSER copy of SASHELP.CLASS (global, so the Workspace's CAS session sees it), paged with table.fetch.
    drop = 'proc casutil; droptable casdata="zz_s2page" incaslib="casuser" quiet; quit;'
    load = (f"cas _s2pg; {drop} libname _s2pg cas caslib=casuser; data _s2pg.zz_s2page(promote=yes); set sashelp.class; run;"
            " libname _s2pg clear; cas _s2pg terminate;")
    _ok(sas, "sas_execute", {"language": "procsql", "sql": load})
    try:
        c = _ok(sas, "sas_query", {"language": "cas", "library": "CASUSER", "max_rows": 5, "tab_id": "zzpage", "run_id": "s_zz_page_2",
                                   "sql": 'select "Name", "Age" from CASUSER."ZZ_S2PAGE"'})
        if c.get("error"):
            pytest.fail(c["error"] + "\n" + c["log"][-2000:])
        assert c["row_count"] == 5 and int(c["total_rows"]) == 19
        p = _ok(sas, "sas_rows", {"run_id": "s_zz_page_2", "offset": 5, "limit": 100})
        assert len(p["rows"]) == 14 and not p["more"]
    finally:
        _ok(sas, "sas_execute", {"language": "procsql", "sql": f"cas _s2pg; {drop} cas _s2pg terminate;"})
    after = _ok(sas, "sas_query", {"language": "procsql", "sql": "select count(*) as n from sashelp.class"})
    assert after["has_result_set"] and not after["error"], after["log"][-1500:]


def test_per_tab_connection(sas: McpSession) -> None:
    """A tab names its SAS Viya connection: runs and lists use that connection's own sessions; an unknown name fails."""
    act = _ok(sas, "sas_connections_state")["active"]
    d = _ok(sas, "sas_query", {"language": "procsql", "sql": "select count(*) as n from sashelp.class", "connection": act})
    assert d["connection"] == act and d["has_result_set"]
    assert _ok(sas, "sas_sources", {"connection": act})["compute_session"]
    r = sas.call("sas_query", {"language": "procsql", "sql": "select 1 as x from sashelp.class", "connection": "zz no such viya"})
    assert r.is_error and "zz no such viya" in r.text


def test_fedsql_pushdown_shows_the_sql_singlestore_received(sas: McpSession) -> None:
    """FedSQL through the automatic SingleStore library: the driver trace gives the SQL text, and SingleStore's plan
    cache knows that very text (Find in Query History)."""
    st = _ok(sas, "sas_libraries", {})
    auto = st.get("auto_libname") or {}
    if not auto.get("assigned"):
        pytest.skip(f"No automatic SingleStore library: {auto.get('note') or auto.get('error')}")
    lib = auto["libref"]
    tables = _ok(sas, "sas_tables", {"library": lib})["tables"]
    if not tables:
        pytest.skip(f"{lib} has no tables")
    t = tables[0]["name"]
    d = _ok(sas, "sas_query", {"language": "fedsql", "sql": f'select count(*) as n from {lib}."{t}" where 1 = 1', "pushdown": True})
    assert not d["error"], d["log"][-2000:]
    pd = d["pushdown"]
    sent = [s for s in pd["sent"] if s["engine"] == "SSTORE"]
    assert sent and "count" in sent[0]["sql"].lower() and t.lower() in sent[0]["sql"].lower(), pd
    found = _ok(sas, "sas_history_lookup", {"sql": sent[0]["sql"], "database": auto.get("database")})
    assert found["found"] and found["plan_cache"], found
    assert found["plan"]["key"].startswith("plan:") and found["plan"]["runs"] >= 1
    # The next FedSQL run doesn't show this run's SQL again (the trace is emptied after every FedSQL run).
    d2 = _ok(sas, "sas_query", {"language": "fedsql", "sql": "select 1 as x from work.\"S2WS_TEST\"", "pushdown": True})
    assert not any("count" in s["sql"].lower() for s in d2.get("pushdown", {}).get("sent", []))


def test_prewarm_starts_the_session_before_the_first_run(sas: McpSession, test_home, sql_dir, record_property) -> None:
    """Opening the Workspace (its sas_prewarm call) starts the SAS session in the background: it's ready without a
    run (automatic SingleStore library and startup code included), and the first PROC SQL doesn't wait for a cold
    session. Its own server with SINGLESTORE_MCP_SAS_PREWARM=1 (the shared test server has it off)."""
    from support import server_env

    s = McpSession(server_env(test_home, sql_dir, {"SINGLESTORE_MCP_SAS_PREWARM": "1"}), read_timeout=600)
    try:
        t0 = time.perf_counter()
        st = _ok(s, "sas_prewarm")
        assert (time.perf_counter() - t0) < 5, "sas_prewarm must answer at once"
        assert st["state"] in ("starting", "ready"), st
        deadline = time.time() + 300
        while st["state"] == "starting" and time.time() < deadline:
            time.sleep(2)
            st = _ok(s, "sas_prewarm", {"start": False})
        assert st["state"] == "ready", st
        assert st["compute_session"] and st.get("compute_ms"), st
        assert "password" not in str(st).lower()
        t = time.perf_counter()
        d = _ok(s, "sas_query", {"language": "procsql", "sql": "select name, age from sashelp.class where age > 15;", "max_rows": 5})
        first_ms = round((time.perf_counter() - t) * 1000)
        assert d["has_result_set"] and not d["error"], d.get("error")
        record_property("prewarm_ms", st.get("ms"))
        record_property("first_proc_sql_ms", first_ms)
        print(f"\nSAS session warmed in the background in {st.get('ms')} ms (Compute {st.get('compute_ms')} ms, "
              f"CAS {st.get('cas_ms')} ms); first PROC SQL after it: {first_ms} ms")
        assert first_ms < 15_000, f"the first PROC SQL still waited {first_ms} ms"
    finally:
        s.call("sas_session_reset", {}, timeout=60)
        s.close()
