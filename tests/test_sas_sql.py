"""SAS in the SQL Editor, offline: wrapping, statement splitting, writes, sastrace / plan parsing, verdicts."""

from __future__ import annotations

import pytest

from singlestore_mcp import sas_sql as s

# Logs as SAS Viya writes them (sastrace=',,,d' sastraceloc=saslog nostsuffix sql_ip_trace=(note, source)).
FULL_LOG = """45   proc sql outobs=5;
45 !                    select cylinders, count(*) as n from s2sash.cars where origin='Asia' group by cylinders; quit;

SSTORE_1: Executed: on connection 2
SELECT * FROM `cars` WHERE 0=1


SSTORE_2: Prepared: on connection 3
SELECT * FROM `cars`


SSTORE_3: Prepared: on connection 4
 select TXT_1.`Cylinders`, COUNT(*) as `n` from `sashelp`.`cars` TXT_1 where TXT_1.`Origin` =
'Asia' group by TXT_1.`Cylinders`


SSTORE_4: Executed: on connection 4
SELECT statement SSTORE_3

ACCESS ENGINE:  SQL statement was passed to the DBMS for fetching data.
NOTE: The PROCEDURE SQL printed page 2.
"""

PARTLY_LOG = """SSTORE_19: Executed: on connection 2
SELECT * FROM `cars` WHERE 0=1


SSTORE_20: Prepared: on connection 3
SELECT * FROM `cars`

SQL_IP_TRACE: Some of the SQL was directly passed to the DBMS.

SSTORE_21: Executed: on connection 3
CREATE RESULT TABLE sascomputeserverb67e_3306_345 AS SELECT  `Make`, `MSRP`, `Origin`, `Model`  FROM
`cars`  WHERE  ( ( `Origin` = 'Asia' ) );


SSTORE_22: Executed: on thread 0
select * from :: sascomputeserverb67e_3306_345 where (partition_id() = 0)


SSTORE_23: Executed: on thread 1
select * from :: sascomputeserverb67e_3306_345 where (partition_id() = 1)


SSTORE_28: Executed: on connection 3
DROP RESULT TABLE sascomputeserverb67e_3306_345

NOTE: No rows were selected.
"""

PULLED_LOG = """SQL_IP_TRACE: None of the SQL was directly passed to the DBMS.

SSTORE_31: Prepared: on connection 4
SELECT DATABASE_NAME, TABLE_NAME, ROW_COUNT FROM information_schema.optimizer_statistics WHERE TABLE_NAME = 'cars'


SSTORE_32: Executed: on connection 4
SELECT statement SSTORE_31


SSTORE_33: Executed: on connection 3
CREATE RESULT TABLE sascomputeserverb67e_3306_345 AS SELECT  `Make`  FROM `cars` ;

"""

CAS_FULL = """Methods for full query plan
----------------------------
        Agg
          SeqScan from SASDP.CARS

Offloaded SQL statement
------------------------

        SELECT `SASDP`.`CARS`.`ORIGIN`, COUNT ( * )  AS `N` FROM `SASDP`.`CARS` GROUP BY `SASDP`.`CARS`.`ORIGIN`

        CAS output table: CASUSER(norjni)._S2RES

NOTE: The SQL statement was fully offloaded to the underlying data source via full pass-through
"""

CAS_PULLED = """Methods for full query plan
----------------------------
        Limit
          HashJoin (INNER)
            SeqScan from SASDP.CARS
            SeqScan from CASUSER._S2RES

NOTE: Performing serial LoadTable action using SAS Data Connector to SingleStore.
"""

FEDSQL_PUSHED = """Methods:
        Limit
          Agg
            SeqScan from S2SASH.{Push Down}.Child 1
"""
FEDSQL_PULLED = """        Agg
          HashJoin (INNER)
            SeqScan from WORK.WORK._X
            SeqScan from S2SASH.sashelp.cars
"""


def test_full_pushdown() -> None:
    v = s.pushdown_verdict("procsql", FULL_LOG)
    assert v["verdict"] == "full"
    assert len(v["sent"]) == 1
    assert v["sent"][0]["sql"].startswith("select TXT_1.`Cylinders`") and "'Asia' group by" in v["sent"][0]["sql"]


def test_partly_pushdown_and_parallel_reads() -> None:
    v = s.pushdown_verdict("procsql", PARTLY_LOG)
    assert v["verdict"] == "partly"
    assert [x["id"] for x in v["sent"]] == ["SSTORE_21"]
    assert v["sent"][0]["parallel_reads"] == 2
    assert "Some of the SQL" in v["notes"][0]


def test_pulled_into_sas() -> None:
    v = s.pushdown_verdict("procsql", PULLED_LOG)
    assert v["verdict"] == "pulled"
    assert [x["id"] for x in v["sent"]] == ["SSTORE_33"]  # the information_schema probe is left out


def test_no_singlestore() -> None:
    assert s.pushdown_verdict("procsql", "NOTE: Table WORK.X created")["verdict"] == "none"


def test_cas_verdicts() -> None:
    full = s.pushdown_verdict("cas", CAS_FULL)
    assert full["verdict"] == "full"
    assert full["sent"][0]["sql"].startswith("SELECT `SASDP`.`CARS`.`ORIGIN`")
    assert s.pushdown_verdict("cas", CAS_PULLED)["verdict"] == "pulled"
    assert s.pushdown_verdict("cas", "nothing")["verdict"] == "none"


def test_fedsql_verdicts() -> None:
    assert s.pushdown_verdict("fedsql", FEDSQL_PUSHED)["verdict"] == "full"
    assert s.pushdown_verdict("fedsql", FEDSQL_PULLED)["verdict"] == "pulled"
    assert s.pushdown_verdict("fedsql", FEDSQL_PUSHED + FEDSQL_PULLED)["verdict"] == "partly"


@pytest.mark.parametrize("text,expect", [
    ("select 1; select 2", ["select 1", "select 2"]),
    ("select ';' from x; select 2;", ["select ';' from x", "select 2"]),
    ("select 'it''s' from x;select 2", ["select 'it''s' from x", "select 2"]),
    ('select "a;b"n from x; ', ['select "a;b"n from x']),
    ("/* a; b */ select 1; -- c; d\nselect 2", ["/* a; b */ select 1", "-- c; d\nselect 2"]),
    ("select 1 # not a comment; select 2", ["select 1 # not a comment", "select 2"]),
    (";;", []),
])
def test_split_statements(text: str, expect: list[str]) -> None:
    assert s.split_statements(text) == expect


@pytest.mark.parametrize("text,plain", [
    ("proc print data=sashelp.class; run;", True),
    ("  /* hi */ data x; set y; run;", True),
    ("%include 'x.sas';", True),
    ("%let a=1;", True),
    ("libname x clear;", True),
    ("select * from sashelp.class", False),
    ("create table work.a as select 1 as x from y", False),
    ("database_size", False),
])
def test_plain_sas(text: str, plain: bool) -> None:
    assert s.is_plain_sas(text) is plain


def test_wrap_proc_sql_select() -> None:
    w = s.wrap("procsql", "select * from s2sash.cars;", library="S2SASH", options={"outobs": 10, "feedback": True},
               max_rows=1000, pushdown=True)
    assert w["kind"] == "select" and w["result"] == ("WORK", "_S2RES") and not w["capped"]
    code = w["code"]
    assert code.splitlines()[0] == s.TRACE_ON and code.splitlines()[-1] == s.TRACE_OFF
    assert "options user=S2SASH;" in code and "options user=work;" in code
    assert "proc sql outobs=10 feedback;" in code
    assert "create table WORK._S2RES as\n  select * from s2sash.cars;" in code
    assert code.count(";;") == 0


def test_wrap_proc_sql_caps_and_statements() -> None:
    w = s.wrap("procsql", "select x into :m from a", max_rows=500)
    assert w["kind"] == "statement" and "outobs=500" in w["code"] and w["capped"]
    assert "create table" not in w["code"]
    w = s.wrap("procsql", "select 1 as x from a", options={"noexec": True})
    assert w["kind"] == "statement" and "noexec" in w["code"]
    w = s.wrap("procsql", "drop table work.a", library="WORK")
    assert "options user" not in w["code"] and "drop table work.a;" in w["code"]


def test_wrap_fedsql_and_cas() -> None:
    w = s.wrap("fedsql", 'select "Make" from s2sash."cars"', pushdown=True)
    assert w["code"].startswith("proc fedsql _method;")
    assert 'drop table WORK."_S2RES" force;' in w["code"]
    c = s.wrap("cas", 'select * from SASDP."CARS"', library="SASDP", pushdown=True)
    action, params = c["actions"][0]
    assert action == "fedSql.execDirect" and params["method"] is True
    assert params["query"].startswith('create table CASUSER."_S2RES" {options replace=true} as select')
    assert "caslib='SASDP'" in c["code"]
    plain = s.wrap("cas", "proc print data=sashelp.class; run;")
    assert plain["kind"] == "sas" and plain["code"] == "proc print data=sashelp.class; run;"
    with pytest.raises(ValueError):
        s.wrap("procsql", " ; ")


@pytest.mark.parametrize("lang,sql,lib,needs", [
    ("procsql", "select * from s2sash.cars", "S2SASH", False),
    ("procsql", "create table work.x as select 1 as a from y", "S2SASH", False),
    ("procsql", "create table x as select 1 as a from y", "WORK", False),
    ("procsql", "create table x as select 1 as a from y", "S2SASH", True),  # one-level name -> USER= library
    ("procsql", "insert into s2sash.cars select * from work.c", "WORK", True),
    ("procsql", "drop table work.a, work.b", None, False),
    ("procsql", "drop table work.a, s2test.b", None, True),
    ("fedsql", "create table x as select 1 as a", "S2SASH", False),  # FedSQL one-level names are WORK
    ("cas", "create table casuser.x as select 1 as a", "SASDP", False),
    ("cas", 'create table "CASUSER(norjni)".x as select 1 as a', "SASDP", False),
    ("cas", "create table x as select 1 as a", "CASUSER", False),
    ("cas", "create table public.x as select 1 as a", "CASUSER", True),
    ("procsql", "data work.a; set sashelp.class; run;", None, False),
    ("procsql", "data s2sash.a; set sashelp.class; run;", None, True),
    ("procsql", "proc sort data=sashelp.class out=sorted; by name; run;", None, False),
    ("procsql", "proc sort data=sashelp.class out=s2test.sorted; by name; run;", None, True),
    ("procsql", "proc print data=sashelp.class; run;", None, False),
    ("procsql", "proc datasets lib=work; delete a; quit;", None, True),
    ("procsql", "x 'rm -rf /';", None, True),
    ("procsql", "proc sql; create table s2test.a as select 1 as x from y; quit;", None, True),
    ("procsql", "proc sql; create table a as select 1 as x from y; quit;", None, False),
    ("cas", "caslib mine path='/tmp';", None, True),
])
def test_needs_confirmation(lang: str, sql: str, lib: str | None, needs: bool) -> None:
    assert (s.needs_confirmation(lang, sql, lib) is not None) is needs


def test_shared_caslib_flag() -> None:
    c = s.needs_confirmation("cas", "insert into public.t select * from casuser.u", "CASUSER")
    assert c and c["shared"] and c["libraries"] == ["PUBLIC"]
    assert not s.needs_confirmation("procsql", "insert into s2.t values(1)", None)["shared"]


def test_mask_secrets() -> None:
    line = "libname x sstore server=h user=u password='p@ss;w' database=d; PWD={abc} pw=xyz"
    out = s.mask_secrets(line)
    assert "p@ss" not in out and "abc" not in out and "xyz" not in out
    assert "database=d" in out


def test_sas_values() -> None:
    assert s.sas_value(0, "DATE9.") == "1960-01-01"
    assert s.sas_value(22000, "YYMMDD10.") == "2020-03-26"
    assert s.sas_value(86400 + 3661, "DATETIME20.") == "1960-01-02 01:01:01"
    assert s.sas_value(3661, "TIME8.") == "01:01:01"
    assert s.sas_value(12.5, "DOLLAR12.2") == 12.5
    assert s.sas_value("x", "DATE9.") == "x"


# ------------------------------------------------------------------ metadata shaping (offline)


def test_library_rows_engine_and_database() -> None:
    from singlestore_mcp import sas_rest

    rows = [
        {"libname": "S2SASH", "engine": "SSTORE", "path": "s2host", "sysname": "DataBase", "sysvalue": "sashelp"},
        {"libname": "S2SASH", "engine": "SSTORE", "path": "s2host", "sysname": "User", "sysvalue": "admin"},
        {"libname": "WORK", "engine": "V9", "path": "/tmp/work", "sysname": "Filename", "sysvalue": "/tmp/work"},
        {"libname": "SASHELP", "engine": "V9", "path": "", "sysname": "", "sysvalue": ""},
    ]
    libs = sas_rest.library_rows(rows)
    assert [x["name"] for x in libs] == ["WORK", "S2SASH", "SASHELP"]
    s2 = libs[1]
    assert s2["singlestore"] and s2["database"] == "sashelp" and s2["server"] == "s2host"
    assert not libs[0]["singlestore"] and libs[0]["database"] is None


def test_caslib_rows_merge_options_and_hide_passwords() -> None:
    from singlestore_mcp.apps.sas_workspace import cas_table_list, caslib_rows

    rows = [
        {"Name": "SASDP", "Type": "singlestore_standard", "Description": "", "Path": "", "Definition": "uid = 'admin'", "Personal": 0, "Active": 0},
        {"Name": "SASDP", "Type": "", "Definition": "pwd = '*****'"},
        {"Name": "SASDP", "Type": "", "Definition": "host = 'h1'"},
        {"Name": "SASDP", "Type": "", "Definition": "database = 'SASDP'"},
        {"Name": "CASUSER(me)", "Type": "PATH", "Path": "/home/me/casuser/", "Definition": "", "Personal": 1, "Active": 1},
    ]
    libs = caslib_rows(rows)
    assert [x["name"] for x in libs] == ["CASUSER(me)", "SASDP"]
    s = libs[1]
    assert s["singlestore"] and s["database"] == "SASDP" and s["server"] == "h1"
    assert "*****" not in repr(libs) and s["secret_options"] == ["pwd"]  # only the option's name, for "Show code"
    assert s["singlestore_kind"] == "standard" and s["badge"] == "S2" and s["badge_style"] == "s2-std"
    tables = cas_table_list([{"Name": "CARS", "Rows": 428, "Columns": 15}],
                            [{"Catalog": "SASDP", "Schema": "SASDP", "Name": "CARS", "Type": "TABLE"},
                             {"Catalog": "SASDP", "Schema": "SASDP", "Name": "CLASS", "Type": "TABLE"},
                             {"Catalog": "SASDP", "Schema": "FANS", "Name": "HR", "Type": "TABLE"}], "SASDP")
    assert [(t["name"], t["loaded"]) for t in tables] == [("CARS", True), ("CLASS", False)]


def test_editor_state_keeps_sas_settings_and_history_language(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    from singlestore_mcp import sql_editor_state as st

    assert st.clean_sas({"library": " S2SASH ", "outobs": "50", "noexec": 1, "pushdown": False, "junk": "x"}) == \
        {"library": "S2SASH", "outobs": 50, "noexec": True, "pushdown": False}
    assert st.clean_sas(None) is None
    assert st.clean_sas({"connection": "Ecodiag", "caslib": "Public"})["connection"] == "Ecodiag"  # a pinned SAS Viya connection
    assert "connection" not in st.clean_sas({"connection": None})
    st.add_history("select 1 as x from sashelp.class", "SASHELP", ok=True, language="procsql")
    st.add_history("select 1", "db", ok=True, language="singlestore")
    entries = st.list_history()["entries"]
    assert entries[0].get("language") is None and entries[1]["language"] == "procsql"


# ------------------------------------------------------------------ SingleStore kinds, Show code, automatic S2 (offline)


@pytest.mark.parametrize("cas_type,kind,badge", [
    ("SingleStore", "speedystore", "SS"), ("singlestore", "speedystore", "SS"),
    ("singlestore_standard", "standard", "S2"), ("PATH", None, None), ("", None, None)])
def test_caslib_singlestore_kind_and_badge(cas_type: str, kind: str | None, badge: str | None) -> None:
    assert s.cas_singlestore_kind(cas_type) == kind
    b = s.singlestore_badge(kind, "SASDP", "h1")
    assert b["badge"] == badge
    if kind == "speedystore":
        assert "SpeedyStore" in b["badge_title"] and "no in-memory copy" in b["badge_title"] and "SASDP" in b["badge_title"]
    if kind == "standard":
        assert "loads the tables into its memory" in b["badge_title"] and "SASDP" in b["badge_title"]


def test_speedystore_caslib_rows_keep_options_for_show_code() -> None:
    from singlestore_mcp.apps.sas_workspace import caslib_rows

    rows = [
        {"Name": "CSASDP", "Type": "SingleStore", "Definition": "host = 'SVC-H'", "Personal": 0, "Active": 0},
        {"Name": "CSASDP", "Type": "", "Definition": "uid = 'admin'"},
        {"Name": "CSASDP", "Type": "", "Definition": "pwd = 'TopSecret1'"},
        {"Name": "CSASDP", "Type": "", "Definition": "database = 'SASDP'"},
        {"Name": "CSASDP", "Type": "", "Definition": "aggregatePushdown = true"},
    ]
    lib = caslib_rows(rows)[0]
    assert lib["badge"] == "SS" and lib["badge_style"] == "ss" and lib["singlestore"]
    assert "TopSecret1" not in repr(lib)
    code = s.caslib_code(lib)
    for text in code.values():
        assert "TopSecret1" not in text and 'pwd="<set password>"' in text and "Reconstructed" in text
    assert 'srctype="singlestore"' in code["statement"] and 'database="SASDP"' in code["statement"]
    assert "aggregatePushdown=true" in code["statement"]  # unquoted booleans stay unquoted
    assert "table.addCaslib" in code["action"] and 'name="CSASDP"' in code["action"]
    assert 'srcType="singlestore"' in code["action"]


def test_path_caslib_code() -> None:
    code = s.caslib_code({"name": "Public", "type": "PATH", "path": "/cas/data/public/", "description": "Shared", "datasource": [],
                          "secret_options": []})
    assert 'caslib Public path="/cas/data/public/" desc="Shared";' in code["statement"]
    assert 'srcType="path"' in code["action"] and 'path="/cas/data/public/"' in code["action"]


def test_libname_code_for_each_engine() -> None:
    from singlestore_mcp import sas_rest

    libs = {x["name"]: x for x in sas_rest.library_rows([
        {"libname": "SASDP", "engine": "SSTORE", "path": "SVC-H", "sysname": "DataBase", "sysvalue": "SASDP"},
        {"libname": "SASDP", "engine": "SSTORE", "path": "SVC-H", "sysname": "User", "sysvalue": "admin"},
        {"libname": "MCP", "engine": "V9", "path": "/data/mcp", "sysname": "Filename", "sysvalue": "/data/mcp"},
        {"libname": "PARQ", "engine": "PARQUET", "path": "/data/pq", "sysname": "File extension list", "sysvalue": "parquet"},
        {"libname": "STG", "engine": "REMOTE9", "path": "/sas9/stg", "sysname": "Accessed through server", "sysvalue": "CAUKI"},
    ])}
    assert libs["SASDP"]["badge"] == "S2" and libs["SASDP"]["singlestore_kind"] == "access" and libs["SASDP"]["user"] == "admin"
    code = s.libname_code(libs["SASDP"])
    assert 'libname SASDP sstore' in code and 'server="SVC-H"' in code and 'user="admin"' in code
    assert 'password="<set password>"' in code and 'database="SASDP"' in code and "port=" in code  # the port note
    assert s.libname_code(libs["MCP"]).endswith('libname MCP "/data/mcp";')
    assert 'libname PARQ parquet "/data/pq";' in s.libname_code(libs["PARQ"])
    assert libs["STG"]["sas9"] and libs["STG"]["remote_server"] == "CAUKI"
    assert "libname STG server=CAUKI slibref=STG;" in s.libname_code(libs["STG"])


def test_auto_libname_code_hides_the_statement_and_reads_its_result() -> None:
    code = s.auto_libname_code("S2", "h1", 3306, "admin", 'pa"ss', "MyDB")
    first, libname = code.splitlines()[0], next(x for x in code.splitlines() if x.startswith("libname"))
    assert "nosource" in first and "nonotes" in first  # before the statement with the password
    assert libname == 'libname S2 sstore server="h1" port=3306 user="admin" password="pa""ss" database="MyDB";'
    assert code.splitlines()[-1] == "options source notes;"
    # The log of a new session's first job also holds the autoexec: only what's between the markers counts.
    ok, errors = s.auto_libname_result(["ERROR: autoexec could not reach CAS", "S2AUTOLIB_BEGIN", "S2AUTOLIB_RC=0"])
    assert ok and errors == []
    ok, errors = s.auto_libname_result(["S2AUTOLIB_BEGIN", "ERROR: Access denied for user admin", "S2AUTOLIB_RC=70006"])
    assert not ok and errors == ["ERROR: Access denied for user admin"]
    assert s.mask_value('ERROR: bad password "pa""ss" and pa"ss', 'pa"ss') == "ERROR: bad password \"********\" and ********"


def test_parse_sas9_libraries() -> None:
    assert s.parse_sas9_libraries("stg, sasdata\nF9=finance") == [("STG", "STG"), ("SASDATA", "SASDATA"), ("F9", "FINANCE")]
    assert s.parse_sas9_libraries("") == []
    with pytest.raises(ValueError):
        s.parse_sas9_libraries("toolonglibref")


def test_automatic_library_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Compute session assigns the automatic SingleStore library with NOSOURCE, only again when what it points at
    changes, and the password never reaches the status or error text."""
    from singlestore_mcp import connections, sas_rest

    p = connections.Profile(name="Zeus", host="h1", port=3306, user="admin", database="MyDB")
    secret = {"pw": "S3cret!pw"}
    monkeypatch.setattr(connections, "active", lambda: p)
    monkeypatch.setattr(connections, "password_for", lambda prof: secret["pw"])
    monkeypatch.setattr(sas_rest.sas_viya, "settings", lambda name=None: {"libref": "S2", "auto_libname": True})
    submitted: list[str] = []
    answer = {"lines": ["S2AUTOLIB_BEGIN", "S2AUTOLIB_RC=0"]}
    c = sas_rest.Compute()
    c.sid = "sid1"
    monkeypatch.setattr(c, "_submit", lambda code, sid: submitted.append(code) or "sid1/jobs/1")
    monkeypatch.setattr(c, "_wait", lambda job: "completed")
    monkeypatch.setattr(c, "_log", lambda job, kind="log": [{"line": x, "type": "normal"} for x in answer["lines"]])
    monkeypatch.setattr(c, "_forget_job", lambda job: None)
    c._ensure_auto()
    assert len(submitted) == 1 and submitted[0].index("nosource") < submitted[0].index("S3cret!pw")
    assert c.auto_lib["assigned"] and c.auto_lib["database"] == "MyDB" and "S3cret!pw" not in repr(c.auto_lib)
    c._ensure_auto()
    assert len(submitted) == 1  # nothing changed
    secret["pw"] = "N3w!pw"  # e.g. a renewed token
    answer["lines"] = ["S2AUTOLIB_BEGIN", "ERROR: login failed with password=N3w!pw", "S2AUTOLIB_RC=1"]
    c._ensure_auto()
    assert len(submitted) == 2 and not c.auto_lib["assigned"]
    assert "N3w!pw" not in c.auto_lib["error"] and "login failed" in c.auto_lib["error"]
    monkeypatch.setattr(sas_rest.sas_viya, "settings", lambda name=None: {"libref": "S2", "auto_libname": False})
    c._ensure_auto()
    assert len(submitted) == 2 and "turned off" in c.auto_lib["note"]


def test_viya_connection_sas9_and_auto_settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SINGLESTORE_MCP_HOME", str(tmp_path))
    from singlestore_mcp import sas_viya

    c = sas_viya.save_connection({"name": "Z", "url": "https://viya.example.com", "sas9_server": "cauki", "sas9_libraries": "stg  sasdata"})
    assert c["auto_libname"] is True and c["sas9_server"] == "CAUKI" and c["sas9_libraries"] == "stg sasdata"
    c = sas_viya.save_connection({**c, "auto_libname": False}, original_name="Z")
    assert sas_viya.settings("Z")["auto_libname"] is False
    with pytest.raises(ValueError):
        sas_viya.save_connection({**c, "sas9_server": "not a name"}, original_name="Z")
