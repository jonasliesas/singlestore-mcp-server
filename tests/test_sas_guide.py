"""SAS in the SQL Editor's in-app assistant and pre-run check: the per-language SAS guide (offline), the tab context
in the question, the SAS function matrix (data/sas_functions.json) and its lookups in Python and in the browser
module (apps/vendor/sas-funcs.js, run in node). ``--sas``: a sample of the matrix's verified rows run again in
SAS (supported / pushed down as recorded)."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest
from support import APPS_DIR, McpSession

from singlestore_mcp import assistant, sas_functions, sas_guide, sas_sql
from singlestore_mcp.apps import sql_editor

JS = APPS_DIR / "vendor" / "sas-funcs.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

S2_CONTEXT = {"library": "S2", "library_info": {"name": "S2", "engine": "SSTORE", "database": "MyDB"},
              "tables": ["CARS", "cars_big"], "options": {"pushdown": True, "outobs": None},
              "columns": {"S2.CARS": [{"name": "Make", "type": "char", "length": 13}, {"name": "MSRP", "type": "num", "format": "DOLLAR8."}]}}


# ----------------------------------------------------------------------------- the guide per language

@pytest.mark.parametrize("lang,must,must_not", [
    ("procsql", ["CALCULATED", "OUTOBS", "'01JAN2024'd", "connect using S2", "no `proc sql;`", "WORK"], ["{options replace=true}` (session"]),
    ("fedsql", ["DATE '2024-01-01'", "No CALCULATED", "S2.\"cars_big\"", "SASHELP", "LIMIT n", "DROP TABLE work.x FORCE"], ["OUTOBS="]),
    ("cas", ["ORDER BY only together with LIMIT", "{options replace=true}", "loaded", "CASUSER", "No INSERT"], ["OUTOBS="]),
])
def test_guide_per_language(lang: str, must: list[str], must_not: list[str]) -> None:
    g = sas_guide.guide(lang)
    for m in must:
        assert m in g, f"{lang} guide lacks {m!r}"
    for m in must_not:
        assert m not in g, f"{lang} guide has {m!r}"
    # Only the tab's own language: no other dialect's rules ride along.
    others = {"procsql": "PROC SQL (SAS SQL dialect)", "fedsql": "FedSQL (PROC FEDSQL, ANSI", "cas": "FedSQL in CAS (fedSql.execDirect"}
    for other, head in others.items():
        assert (head in g) == (other == lang)
    assert len(g) < 9000, "keep the guide compact (tokens)"
    assert "Functions in" in g and "pushed down" in g  # the function matrix's lists


def test_no_guide_for_singlestore() -> None:
    assert sas_guide.guide("singlestore") == ""
    p = assistant.build_prompt("why slow?", "SASDP", "SELECT 1", False, None)
    assert p.startswith("Current database: SASDP") and "SAS guide" not in p


def test_build_prompt_carries_context_and_guide() -> None:
    p = assistant.build_prompt("top 10 makes", "MyDB", "select * from s2.cars", True, None, language="procsql", sas=S2_CONTEXT)
    assert "runs PROC SQL in SAS" in p and "SingleStore database MyDB" in p and "Tables in S2: CARS, cars_big" in p
    assert "Columns of S2.CARS: Make char(13), MSRP num DOLLAR8." in p
    assert "SAS guide for PROC SQL" in p and p.index("SAS guide") < p.index("Question: top 10 makes")
    assert "Selected SQL in the editor" in p
    q = assistant.build_prompt("and by origin?", "MyDB", None, False, None, language="procsql", sas=S2_CONTEXT, guide=False)
    assert "SAS guide" not in q and "Columns of S2.CARS" in q


def test_context_text_caslib_loaded_flags() -> None:
    t = sas_guide.context_text("cas", {"library": "C_MYDB", "library_info": {"name": "C_MYDB", "type": "SingleStore",
                                                                              "singlestore_kind": "speedystore"},
                                       "tables": [{"name": "CARS", "loaded": False}, {"name": "HMEQ", "loaded": True}]})
    assert "caslib: C_MYDB" in t and "SpeedyStore" in t and "CARS (not loaded)" in t and "HMEQ," not in t.replace("HMEQ.", "")
    assert sas_guide.context_text("cas", None).startswith("The tab runs FedSQL in CAS; its caslib: CASUSER")


def test_guide_once_per_session_and_language(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(assistant, "ask", lambda eid, prompt, deliver, profile=None, kind="sql_editor": sent.append(prompt))
    eid = "e-zzguide"
    monkeypatch.setitem(assistant._sessions, eid, "session-1")  # an existing Claude session
    assistant._guided.pop(eid, None)
    ask = lambda q, lang: sql_editor.sql_editor_ask(eid, q, "MyDB", language=lang, sas=S2_CONTEXT)  # noqa: E731
    try:
        ask("top 10 makes", "procsql")
        ask("and per origin?", "procsql")
        ask("same in FedSQL", "fedsql")
        ask("a SingleStore question", "singlestore")
        ask("back to PROC SQL", "procsql")
    finally:
        assistant._guided.pop(eid, None)
    has = ["SAS guide for" in p for p in sent]
    assert has == [True, False, True, False, True]
    assert "SAS guide for FedSQL" in sent[2]
    # A new session (no Claude session yet) always gets the guide.
    assistant._sessions.pop(eid, None)
    assert assistant.needs_guide(eid, "fedsql")


# ----------------------------------------------------------------------------- write check: explicit pass-through

def test_explicit_pass_through_is_a_read() -> None:
    code = ("proc sql;\n  connect using S2;\n  select * from connection to S2 (SELECT Origin, COUNT(*) FROM CARS GROUP BY Origin);\n"
            "  disconnect from S2;\nquit;")
    assert sas_sql.needs_confirmation("procsql", code, None) is None
    assert sas_sql.sql_targets("connect using S2", "WORK") is None
    assert sas_sql.sql_targets("disconnect from S2", "WORK") is None
    assert sas_sql.sql_targets("execute (delete from CARS) by S2", "WORK") == ["?"]
    assert sas_sql.needs_confirmation("procsql", "proc sql; connect using S2; execute (drop table x) by S2; quit;", None)


def test_fedsql_verdict_trusts_the_driver_trace() -> None:
    """FedSQL's plan says "Push Down" for GROUP BY ... ORDER BY ... LIMIT (and LIMIT alone) although SingleStore got
    only the table read (seen on Zeus): with a driver trace of plain reads the verdict is "pulled"."""
    plan = "Methods for full query plan\n  SeqScan from {Push Down}.Child 1\n"
    plain = [{"id": "fedsql_1", "engine": "SSTORE", "sql": "select `MyDB`.`CARS`.`Make` from `MyDB`.`CARS`"}]
    grouped = [{"id": "fedsql_1", "engine": "SSTORE",
                "sql": "select `MyDB`.`CARS`.`Origin`, COUNT(*) as `N` from `MyDB`.`CARS` group by `MyDB`.`CARS`.`Origin`"}]
    v = sas_sql.pushdown_verdict("fedsql", plan, traced=plain)
    assert v["verdict"] == "pulled" and any("plain table reads" in n for n in v["notes"])
    assert sas_sql.pushdown_verdict("fedsql", plan, traced=grouped)["verdict"] == "full"
    assert sas_functions.pushdown_of("fedsql", {"pushdown": {"verdict": "full", "sent": plain}}) == "no"


# ----------------------------------------------------------------------------- the function matrix

def test_matrix_shape() -> None:
    m = sas_functions.matrix()
    assert m["sources"] and m["test_tables"]["procsql"]["cars"]["t"] == "S2.CARS"
    names = [f["name"] for f in m["functions"]]
    assert len(names) == len(set(names)) >= 60
    for f in m["functions"]:
        assert f["category"] and f["test"] and f["kind"] in ("scalar", "agg"), f["name"]
        for d in sas_functions.DIALECTS:
            e = f.get(d) or {}
            if e:
                assert e.get("how") in ("verified", "documented"), (f["name"], d)
                assert e.get("pushdown") in ("yes", "no", "partly", "n/a", "unknown"), (f["name"], d)


@pytest.mark.parametrize("name,dialect,supported,pushdown", [
    ("UPCASE", "procsql", True, "yes"),
    ("YEAR", "procsql", True, "yes"),
    ("CATX", "procsql", True, "no"),
    ("CATX", "fedsql", False, "n/a"),
    ("CATX", "cas", False, "n/a"),
    ("INTNX", "procsql", True, "no"),
    ("COUNT(DISTINCT)", "fedsql", True, "yes"),
])
def test_matrix_known_rows(name: str, dialect: str, supported: bool, pushdown: str) -> None:
    e = sas_functions.by_name(name)[dialect]
    assert (e["supported"], e["pushdown"]) == (supported, pushdown), e


def test_functions_in_skips_strings_comments_and_identifiers() -> None:
    sql = "select upcase(make), 'catx(' as s /* intnx( */, \"put(\" from s2.cars where year(d) in (1, 2) and x || y"
    assert sas_functions.functions_in(sql, "procsql") == ["UPCASE", "YEAR", "||"]
    assert sas_functions.functions_in('select "UPCASE"(x) from t', "procsql") == []  # a PROC SQL string, no call
    assert sas_functions.functions_in("select count( distinct make) from t", "fedsql") == ["COUNT(DISTINCT)", "COUNT"]


def test_check_unsupported_and_pushdown() -> None:
    sql = "select catx(' - ', make, model) as mm, upcase(make) from s2.cars"
    assert sas_functions.check(sql, "procsql", singlestore=False) == []
    found = sas_functions.check(sql, "procsql", singlestore=True)
    assert [(f["function"], f["issue"]) for f in found] == [("CATX", "no_pushdown")]
    found = sas_functions.check(sql, "fedsql", singlestore=False)
    assert ("CATX", "unsupported") in [(f["function"], f["issue"]) for f in found]
    assert sas_functions.check(sql, "singlestore", True) == []


def test_editor_tool_serves_the_matrix() -> None:
    funcs = sql_editor.sql_editor_sas_functions().structured_content["functions"]
    assert funcs["UPCASE"]["procsql"] == {"s": True, "p": "yes"} and funcs["CATX"]["fedsql"]["s"] is False


def js(expr: str) -> Any:
    code = f"const F = require({json.dumps(str(JS))});\nprocess.stdout.write(JSON.stringify((() => {{ {expr} }})()));"
    r = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(r.stdout)


@needs_node
@pytest.mark.parametrize("sql,lang,s2", [
    ("select catx(' - ', make, model) as mm, upcase(make) from s2.cars", "procsql", True),
    ("select catx(' - ', \"Make\", \"Model\") from S2.CARS", "fedsql", True),
    ("select intnx('month', month, 0, 'b') as m, put(month, yymmn6.) from s2.prdsal", "procsql", True),
    ("select upper(x), year(d) || 'x' from CASUSER.T where y like 'a%'", "cas", False),
    ("select 'catx(' as s from t -- intnx(\n", "procsql", True),
])
def test_js_check_matches_python(sql: str, lang: str, s2: bool) -> None:
    funcs = json.dumps(sas_functions.compact())
    got = js(f"return F.check({json.dumps(sql)}, {json.dumps(lang)}, {funcs}, {json.dumps(s2)});")
    want = sas_functions.check(sql, lang, s2)
    assert [(g["function"], g["issue"]) for g in got] == [(w["function"], w["issue"]) for w in want]


@needs_node
def test_js_message() -> None:
    funcs = json.dumps(sas_functions.compact())
    msg = js(f"return F.message(F.check(\"select catx(',', a, b), intnx('month', d, 1) from s2.cars\", 'procsql', {funcs}, true), 'procsql');")
    assert "CATX" in msg and "INTNX" in msg and "not pushed down to SingleStore" in msg
    msg = js(f"return F.message(F.check(\"select catx(',', a, b) from S2.CARS\", 'fedsql', {funcs}, false), 'fedsql');")
    assert msg.startswith("CATX") and "not available in FedSQL" in msg
    assert js(f"return F.message(F.check('select upcase(a) from s2.cars', 'procsql', {funcs}, true), 'procsql');") is None


# ----------------------------------------------------------------------------- --sas: verified rows still hold

VERIFY_SAMPLE = [("UPCASE", "procsql"), ("CATX", "procsql"), ("YEAR", "fedsql"), ("CATX", "fedsql"), ("UPPER", "cas"), ("CATX", "cas")]


@pytest.mark.cluster
@pytest.mark.sas
@pytest.mark.parametrize("name,dialect", VERIFY_SAMPLE)
def test_matrix_rows_hold_in_sas(sas: McpSession, name: str, dialect: str) -> None:
    fn = sas_functions.by_name(name)
    want = fn[dialect]
    assert want.get("how") == "verified"
    sql = sas_functions.statement(fn, dialect)
    r = sas.call("sas_query", {"language": dialect, "sql": sql, "library": "CASUSER" if dialect == "cas" else None,
                               "max_rows": 5, "history": False, "pushdown": True}, timeout=600)
    d = r.data if isinstance(r.data, dict) else {}
    assert (not d.get("error")) == want["supported"], (sql, d.get("error"))
    if want["supported"] and want["pushdown"] in ("yes", "no"):
        assert sas_functions.pushdown_of(dialect, d) == want["pushdown"], (sql, d.get("pushdown"))
