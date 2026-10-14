"""Tools against the cluster: model-visible tools (incl. Claude's 50,000-character result limit), app-only
tools, error handling and safety guards, and local writes. Read-only on the cluster."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import DB, RESULT_LIMIT, TABLE, McpSession, Result

pytestmark = pytest.mark.cluster


def _check_limit(r: Result) -> None:
    assert max(r.text_chars, r.struct_chars) <= RESULT_LIMIT, (
        f"{r.name} result is over Claude's {RESULT_LIMIT}-character limit: text {r.text_chars}, structured {r.struct_chars}")


# ----------------------------------------------------------------------------- model-visible tools

MODEL_CALLS = [
    ("list_databases", {}, ""),
    ("list_tables", {"database": DB}, ""),
    ("describe_table", {"table": TABLE, "database": DB}, ""),
    ("run_sql", {"sql": f"SELECT * FROM {TABLE}", "database": DB}, "200 rows default"),
    ("run_sql", {"sql": f"SELECT * FROM {TABLE}", "database": DB, "max_rows": 5}, "max_rows 5"),
    ("list_pipelines", {"database": DB}, ""),
    ("list_connections", {}, ""),
    ("browser_link", {"tool": "schema_explorer", "arguments": {"database": DB, "table": TABLE}}, ""),
    ("pipeline_monitor", {}, ""),
    ("query_grid", {"sql": f"SELECT * FROM {TABLE}", "database": DB}, ""),
    ("schema_explorer", {}, ""),
    ("schema_explorer", {"database": DB, "table": TABLE}, "with table"),
    ("cleanup_work_tables", {}, ""),
    ("cluster_monitor", {}, ""),
    ("sql_editor", {"database": DB}, ""),
    ("notebook", {"database": DB}, ""),
    ("connections_window", {}, ""),
    ("query_history", {}, ""),
    ("query_history", {"tab": "advisor"}, "advisor tab"),
    ("query_advisor_report", {}, ""),
    ("alerts", {}, ""),
    ("workload", {}, ""),
    ("workload_list", {}, ""),
]


@pytest.mark.parametrize("name,args", [(n, a) for n, a, _ in MODEL_CALLS],
                         ids=[f"{n}-{label}" if label else n for n, _, label in MODEL_CALLS])
def test_model_tool(cluster: McpSession, name: str, args: dict) -> None:
    r = cluster.call(name, args)
    assert not r.is_error, r.text[:800]
    _check_limit(r)


def test_run_sql_max_rows_honored(cluster: McpSession) -> None:
    d = cluster.ok("run_sql", {"sql": f"SELECT * FROM {TABLE}", "database": DB, "max_rows": 5})
    assert isinstance(d, dict) and len(d.get("rows", [])) == 5, str(d)[:300]


def test_run_sql_large_result_stays_under_limit(cluster: McpSession) -> None:
    r = cluster.call("run_sql", {"sql": "SELECT a.* FROM sashelp.cars a, sashelp.cars b LIMIT 5000", "max_rows": 5000})
    assert not r.is_error, r.text[:500]
    _check_limit(r)


def test_pipelines_status_ddl_errors(cluster: McpSession) -> None:
    pl = cluster.ok("list_pipelines", {"database": DB})
    rows = (pl.get("rows") or [])[:3] if isinstance(pl, dict) else []
    if not rows:
        pytest.skip(f"no pipelines in {DB}")
    failures = []
    for row in rows:
        name = list(row.values())[0]
        for tool in ("pipeline_status", "get_pipeline_ddl", "pipeline_errors"):
            r = cluster.call(tool, {"pipeline_name": name, "database": DB})
            if r.is_error:
                failures.append(f"{tool}({name}): {r.text[:200]}")
            elif tool != "pipeline_errors" and max(r.text_chars, r.struct_chars) > RESULT_LIMIT:
                failures.append(f"{tool}({name}): {r.chars} chars")
    assert not failures, failures


# ----------------------------------------------------------------------------- errors and safety guards

ERROR_CASES = [
    ("syntax error", "run_sql", {"sql": "SELEC 1"}),
    ("missing table", "run_sql", {"sql": "SELECT * FROM zz_no_such_table", "database": DB}),
    ("missing database", "run_sql", {"sql": "SELECT 1", "database": "zz_no_such_db"}),
    ("identifier injection", "describe_table", {"table": "x`; DROP TABLE y", "database": DB}),
    ("not CREATE PIPELINE", "create_pipeline", {"create_pipeline_sql": "DROP TABLE CARS", "database": DB}),
    ("not ALTER PIPELINE", "alter_pipeline", {"alter_pipeline_sql": "DELETE FROM CARS", "database": DB}),
    ("query_grid write refused", "query_grid", {"sql": "DELETE FROM CARS WHERE 1=0", "database": DB}),
    ("schema missing table", "schema_explorer_table", {"database": DB, "table": "zz_no_such_table"}),
    ("preview bad identifier", "schema_explorer_preview", {"database": DB, "table": "x`y"}),
    ("history unknown key", "query_history_event", {"key": "0:0:0"}),
    ("tuning garbage key", "query_history_tuning", {"key": "garbage"}),
    ("grid unknown result", "query_grid_rows", {"result_id": "nope"}),
    ("notebook missing file", "notebook_open", {"name": "zz_missing.ipynb"}),
    ("notebook path traversal", "notebook_open", {"name": "..\\..\\Windows\\win.ini"}),
    ("sql file path traversal", "sql_editor_open_file", {"name": "../../secret.sql"}),
    ("sql save outside user folder", "sql_editor_save_file", {"name": "C:\\Windows\\zz_escape.sql", "sql": "SELECT 1"}),
    ("browse outside folder", "files_browse", {"path": "C:\\Windows\\System32", "kind": "sql"}),
    ("kernel bad action", "notebook_kernel_control", {"notebook_id": "zz", "action": "explode"}),
    ("notebook bad cell type", "notebook_run", {"notebook_id": "zz", "cell_type": "cobol", "source": "x"}),
    ("missing connection", "use_connection", {"name": "zz no such connection"}),
    ("unknown app", "browser_link", {"tool": "zz_not_an_app"}),
    ("compare bad table name", "query_history_compare_candidates", {"table": "nodot", "new_table": "x"}),
    ("workload unknown run", "workload_status", {"run_id": "zz-no-such-run"}),
    ("workload stop unknown run", "workload_stop", {"run_id": "zz-no-such-run"}),
    ("workload compare unknown runs", "workload_compare", {"base_run_id": "zz-a", "new_run_id": "zz-b"}),
]


@pytest.mark.parametrize("name,args", [(n, a) for _, n, a in ERROR_CASES], ids=[c[0] for c in ERROR_CASES])
def test_fails_cleanly(cluster: McpSession, name: str, args: dict) -> None:
    r = cluster.call(name, args)
    assert r.is_error, f"expected an error, got: {r.text[:400]}"
    assert r.text.strip(), "error without a message"
    assert "Traceback" not in r.text, r.text[:800]


CONFIRM_CASES = [
    ("editor DROP", "sql_editor_query", {"sql": "DROP TABLE CARS", "database": DB}),
    ("editor stacked", "sql_editor_query", {"sql": "SELECT 1; DROP TABLE CARS", "database": DB}),
    ("notebook DELETE", "notebook_run", {"notebook_id": "zz", "cell_type": "sql", "source": "DELETE FROM CARS WHERE 1=0", "database": DB}),
]


@pytest.mark.parametrize("name,args", [(n, a) for _, n, a in CONFIRM_CASES], ids=[c[0] for c in CONFIRM_CASES])
def test_write_asks_for_confirmation(cluster: McpSession, name: str, args: dict) -> None:
    d = cluster.ok(name, args)
    assert isinstance(d, dict) and d.get("needs_confirmation"), f"ran or refused without asking: {str(d)[:400]}"


def test_missing_pipeline_status_is_empty(cluster: McpSession) -> None:
    d = cluster.ok("pipeline_status", {"pipeline_name": "zz_no_pipe", "database": DB})
    assert isinstance(d, dict) and d.get("rows") == [], str(d)[:300]


# ----------------------------------------------------------------------------- app-only tools (read-only)

APP_CALLS = [
    ("app_page", {"uri": "ui://singlestore/sql-editor.html"}, ""),
    ("pipeline_monitor_data", {}, ""),
    ("schema_explorer_databases", {}, ""),
    ("schema_explorer_tables", {"database": DB}, ""),
    ("schema_explorer_tables", {"database": "sashelp"}, "354 tables"),
    ("schema_explorer_table", {"database": DB, "table": TABLE}, ""),
    ("schema_explorer_preview", {"database": DB, "table": TABLE, "limit": 100}, ""),
    ("schema_explorer_cleanup_scan", {}, ""),
    ("schema_explorer_cleanup_drop", {"tables": [{"database": DB, "name": "zz_regression_none"}], "views": [], "dry_run": True}, "dry run"),
    ("cluster_monitor_data", {}, ""),
    ("cluster_monitor_data", {"include_internal": True}, "internal"),
    ("sql_editor_query", {"sql": "SELECT Make, COUNT(*) n FROM CARS GROUP BY Make ORDER BY n DESC", "database": DB}, ""),
    ("sql_editor_query", {"sql": "SELECT 1 AS a; SELECT 2 AS b", "database": DB}, "two statements"),
    ("sql_editor_query", {"sql": "SELECT NULL n, CAST(1.5 AS DECIMAL(10,2)) d, NOW() t, X'00ff' b, JSON_BUILD_OBJECT('a',1) j", "database": DB}, "types"),
    ("sql_editor_execute", {"sql": "SELECT COUNT(*) FROM CARS", "database": DB}, "select via execute"),
    ("sql_editor_schema", {"database": DB}, ""),
    ("sql_editor_files", {}, ""),
    ("files_browse", {"path": "", "kind": "sql"}, ""),
    ("sql_editor_inbox", {"editor_id": "e-zzreg", "after": 0}, ""),
    ("sql_editor_assistant", {}, ""),
    ("connections_state", {}, ""),
    ("sas_viya_state", {}, ""),
    ("sas_viya_test", {}, ""),
    ("query_history_data", {"min_ms": 1000, "hours": 168, "limit": 500}, "7 days 500"),
    ("query_history_advisor", {}, ""),
    ("query_history_advisor", {"refresh": True}, "refresh"),
    ("query_history_assistant", {}, ""),
    ("query_history_trends", {"days": 30}, ""),
    ("alerts_data", {}, ""),
    ("alerts_badge", {}, ""),
    ("alerts_check_now", {}, ""),
    ("notebook_environment", {}, ""),
    ("notebook_packages", {}, ""),
    ("notebook_files", {}, ""),
    ("workload_overview", {}, ""),
    ("workload_test_statement", {"sql": "SELECT COUNT(*) FROM CARS WHERE Cylinders > {{int:2:8}}", "database": DB}, "placeholder"),
    ("workload_chat_context", {"workload": {"name": "zz", "database": DB, "settings": {}, "statements": [
        {"id": "s1", "name": "n", "sql": "SELECT * FROM CARS WHERE Cylinders = {{int:4:8}}", "per_minute": 6}]}}, ""),
]


def test_workload_chat_context_has_table_definitions(cluster: McpSession) -> None:
    w = {"name": "zz", "database": DB, "settings": {}, "statements": [
        {"id": "s1", "name": "n", "sql": f"SELECT Make FROM {TABLE} WHERE Cylinders = {{{{int:4:8}}}}", "per_minute": 6}]}
    text = cluster.ok("workload_chat_context", {"workload": w})["text"]
    assert f"{DB}.{TABLE} (~" in text and "CREATE TABLE" in text, text[-1500:]
    assert len(text) < 50_000
    short = cluster.ok("workload_chat_context", {"workload": w, "limit": 2000})["text"]
    assert len(short) <= 2000


@pytest.mark.parametrize("name,args", [(n, a) for n, a, _ in APP_CALLS],
                         ids=[f"{n}-{label}" if label else n for n, _, label in APP_CALLS])
def test_app_tool(cluster: McpSession, name: str, args: dict) -> None:
    r = cluster.call(name, args)
    assert not r.is_error, r.text[:800]


@pytest.mark.parametrize("sql,expect", [
    ("SELECT '100%' AS pct, 'a%%b' AS s", "100%"),
    ("SELECT 'æøå 你好 😀' AS utf8", "æøå 你好 😀"),
], ids=["literal %", "unicode"])
def test_sql_editor_values_round_trip(cluster: McpSession, sql: str, expect: str) -> None:
    d = cluster.ok("sql_editor_query", {"sql": sql, "database": DB})
    assert expect in json.dumps(d, ensure_ascii=False), str(d)[:400]


def test_workload_test_statement_refuses_writes(cluster: McpSession) -> None:
    d = cluster.ok("workload_test_statement", {"sql": "DELETE FROM CARS WHERE 1=0", "database": DB})
    assert d.get("ran") is False and d.get("read_only") is False, d


def test_query_grid_rows(cluster: McpSession) -> None:
    g = cluster.ok("query_grid", {"sql": "SELECT * FROM CARS", "database": DB})
    rid = g.get("result_id") if isinstance(g, dict) else None
    assert rid, f"query_grid returned no result_id: keys={list(g)[:12] if isinstance(g, dict) else type(g)}"
    rows = cluster.ok("query_grid_rows", {"result_id": rid})
    assert rows.get("rows"), str(rows)[:300]


def test_connection_test_active(cluster: McpSession) -> None:
    active = cluster.ok("list_connections")["active"]
    d = cluster.ok("connection_test", {"name": active})
    assert d.get("ok"), d


def test_query_history_event_and_tuning(cluster: McpSession) -> None:
    d = cluster.ok("query_history_data", {"min_ms": 1000, "hours": 168, "limit": 500})
    events = d.get("events") or []
    if not events:
        pytest.skip("no traced queries in the last 7 days")
    k = events[0]["key"]
    cluster.ok("query_history_event", {"key": k})
    cluster.ok("query_history_tuning", {"key": k})


def test_query_history_trend_runs(cluster: McpSession) -> None:
    t = cluster.ok("query_history_trends", {"days": 30})
    days = [x["t"] for x in (t.get("daily") or [])]
    hours = [x["t"] for x in (t.get("hourly") or [])]
    if not days:
        pytest.skip("no trend data")
    cluster.ok("query_history_trend_runs", {"bucket": days[-1]})
    if hours:
        cluster.ok("query_history_trend_runs", {"bucket": hours[-1], "failed_only": True})


# ----------------------------------------------------------------------------- local writes (test home / temp SQL folder)

def test_sql_file_round_trip(cluster: McpSession, sql_dir: Path) -> None:
    name = "zz_regression_test.sql"
    cluster.ok("sql_editor_save_file", {"name": name, "sql": "SELECT 'æøå' AS x;"})
    assert (sql_dir / name).read_text(encoding="utf-8").startswith("SELECT 'æøå'")
    d = cluster.call("sql_editor_save_file", {"name": name, "sql": "SELECT 2;"}).data
    assert not (isinstance(d, dict) and d.get("saved")), "save overwrote an existing file without overwrite=true"
    assert "æøå" in (sql_dir / name).read_text(encoding="utf-8")
    d = cluster.ok("sql_editor_open_file", {"name": name})
    assert "æøå" in json.dumps(d, ensure_ascii=False), "SQL file round trip lost characters"
    files = cluster.ok("sql_editor_files")
    assert name in json.dumps(files, ensure_ascii=False)


def test_notebook_save_and_open(cluster: McpSession, sql_dir: Path) -> None:
    name = "zz_regression_test.ipynb"
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {},
          "cells": [{"cell_type": "code", "metadata": {}, "source": "print('æøå')", "outputs": [], "execution_count": None}]}
    cluster.ok("notebook_save", {"name": name, "notebook": nb, "overwrite": True})
    assert (sql_dir / name).is_file()
    d = cluster.ok("notebook_open", {"name": name})
    assert "æøå" in json.dumps(d, ensure_ascii=False)
    cluster.err("notebook_save", {"name": name, "notebook": {"x": 1}, "overwrite": True})
    listing = cluster.ok("notebook_files")
    assert name in json.dumps(listing, ensure_ascii=False)


def test_alerts_update_persists_and_restores(cluster: McpSession, test_home: Path) -> None:
    a = cluster.ok("alerts_data")
    rules, settings = a["rules"], a["settings"]
    changed = json.loads(json.dumps(rules))
    changed["long_running"]["seconds"] = 61
    try:
        cluster.ok("alerts_update", {"rules": changed, "settings": settings})
        assert cluster.ok("alerts_data")["rules"]["long_running"]["seconds"] == 61, "alerts_update didn't persist"
        assert (test_home / "alerts.json").is_file(), "alerts.json not written in the test home"
    finally:
        cluster.ok("alerts_update", {"rules": rules, "settings": settings})
    assert cluster.ok("alerts_data")["rules"]["long_running"]["seconds"] == rules["long_running"]["seconds"]
    cluster.err("alerts_update", {"rules": {"memory": {"percent": "abc"}}, "settings": {}})


def test_connection_save_test_delete(cluster: McpSession) -> None:
    name = "zz regression"
    prof = {"name": name, "host": "127.0.0.1", "port": 1, "user": "nobody", "database": "", "auth": "password"}
    try:
        cluster.ok("connection_save", {"profile": prof, "password": "zz-not-a-real-password", "activate": False})
        d = cluster.ok("connection_test", {"name": name})
        assert not d.get("ok"), "a connection to 127.0.0.1:1 should fail"
    finally:
        cluster.call("connection_delete", {"name": name})
    st = cluster.ok("connections_state")
    assert not any(x["name"] == name for x in st["connections"]), "test connection still listed after delete"
    # the active connection is untouched
    assert cluster.ok("list_connections")["active"] == "Environment variables"


def test_builtin_connection_is_protected(cluster: McpSession) -> None:
    cluster.err("connection_delete", {"name": "Environment variables"})
