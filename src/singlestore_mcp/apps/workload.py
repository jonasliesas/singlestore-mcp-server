"""Workload app: define test workloads (SQL statements and how often each runs), run them, compare runs.

The runner lives in ``singlestore_mcp.workload``; this module is the tools and the view. Claude can define,
run and compare workloads with the model-visible tools, and the Workload view does the same by hand.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import workload as wl
from ._core import APP_ONLY, apps, register_app, tool_result, with_browser_link

URI = "ui://singlestore/workload.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
LOCAL_DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
# Runs the workload's statements against the cluster (read-only unless the workload allows writes).
RUNS_QUERIES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

register_app(
    URI,
    "workload.html",
    name="Workload",
    description="Test workloads: SQL statements with how often each runs, replayed against the cluster; compare runs",
)


def _overview() -> dict[str, Any]:
    return {"workloads": wl.list_workloads(), "running": wl.running(), "runs": wl.list_runs()[:100]}


def _overview_text(d: dict[str, Any]) -> str:
    lines = [f"{len(d['workloads'])} saved workload(s); shown in the Workload app."]
    for w in d["workloads"][:20]:
        last = w.get("last_run")
        lines.append(f"- {w['name']}: {len(w['statements'])} statement(s), {w['settings']['pacing']} pacing, "
                     f"{w['settings']['concurrency']} connections, {w['settings']['duration_s']} s"
                     + (f"; last run {last['id']} ({last.get('label') or 'no label'}): p95 {last['totals'].get('p95_ms')} ms, "
                        f"{last['totals'].get('qps')} per s" if last and last.get("totals") else ""))
    if d.get("running"):
        r = d["running"]
        lines.append(f"Running now: {r['workload']} ({r['id']}), {r['elapsed_s']} of {r['duration_s']} s.")
    return "\n".join(lines)


# ------------------------------------------------------------------ model-visible

@apps.tool(resource_uri=URI, title="Workload", annotations=READ_ONLY)
def workload(name: str | None = None) -> CallToolResult:
    """Open the Workload view: define test workloads (SQL statements, each with how often it runs per minute),
    run them against the cluster with N connections for a set time, watch throughput and latency live, and
    compare two runs statement by statement (e.g. before and after a new sort key or index).

    Args:
        name: Workload to select (optional).

    The result includes ``browser_url``: post it as a clickable link right under the app.
    """
    d = _overview() | {"selected": name}
    return tool_result(with_browser_link(_overview_text(d), d, "workload", {"name": name} if name else {}), d)




@apps.tool(resource_uri=URI, annotations=RUNS_QUERIES)
def workload_run(name: str, label: str = "", duration_s: int | None = None, concurrency: int | None = None) -> CallToolResult:
    """Start a run of a saved workload (one run at a time). Returns the run id; follow it with workload_status
    and compare runs with workload_compare. Give runs a label such as "baseline" or "after sort key".

    Args:
        name: The workload.
        label: A short label for this run.
        duration_s: Override the workload's duration for this run.
        concurrency: Override the number of connections for this run.
    """
    r = wl.start(name, label, duration_s, concurrency)
    return tool_result(f"Started run {r['id']} of {name!r} for {r['duration_s']} s with {r['settings']['concurrency']} "
                       "connection(s). Check it with workload_status.", r)




@apps.tool(resource_uri=URI, annotations=READ_ONLY)
def workload_compare(base_run_id: str, new_run_id: str) -> CallToolResult:
    """Compare two runs of a workload statement by statement: p50 / p95 before and after, change in percent,
    throughput, errors, and whether the SQL or the row counts changed. Use it to check a tuning change:
    run a baseline, change the table (sort key, shard key, index ...), run again, compare."""
    c = wl.compare_runs(base_run_id, new_run_id)
    return tool_result(wl.compare_summary(c), c)


# ------------------------------------------------------------------ app-only

@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_overview() -> CallToolResult:
    """Workloads, recent runs and the running one, for the Workload view."""
    d = _overview()
    return tool_result(_overview_text(d), d)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_WRITE)
def workload_save(workload: dict[str, Any], original_name: str | None = None) -> CallToolResult:
    """Save a workload from the Workload view (create, replace or rename)."""
    w = wl.save(workload, original_name)
    return tool_result(f"Saved {w['name']}.", {"workload": w, **_overview()})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_DELETE)
def workload_delete(name: str, runs_too: bool = False) -> CallToolResult:
    """Delete a saved workload (and optionally its saved runs)."""
    r = wl.delete(name, runs_too)
    return tool_result(f"Deleted {name}.", {**r, **_overview()})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_DELETE)
def workload_delete_run(run_id: str) -> CallToolResult:
    """Delete one saved run."""
    n = wl.delete_run(run_id)
    return tool_result(f"Deleted {n} run(s).", {"deleted": n, **_overview()})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_from_history(hours: float = 24, top: int = 10, min_ms: int = 0) -> CallToolResult:
    """The heaviest read-only queries of the last ``hours`` from the query history, as workload statements
    (runs per minute = how often they ran). Nothing is saved or run."""
    stmts = wl.from_history(hours, top, min_ms)
    return tool_result(f"{len(stmts)} statement(s) from the query history.", {"statements": stmts})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_test_statement(sql: str, database: str | None = None) -> CallToolResult:
    """Run one statement once (placeholders filled in) to check it: time, rows or the error. Read-only statements
    only; others are only checked for placeholders."""
    import random
    import time

    from .. import compare
    from ..db import db

    rendered = wl.render(sql.strip().rstrip(";"), random.Random())
    if not compare.read_only(rendered):
        return tool_result("Not run: only SELECT / WITH statements are test-run here.",
                           {"ran": False, "sql": rendered, "read_only": False})
    started = time.perf_counter()
    columns, rows, _ = db.execute(rendered, database=database or None, max_rows=1001)
    ms = round((time.perf_counter() - started) * 1000, 1)
    return tool_result(f"{len(rows)} row(s) in {ms} ms", {"ran": True, "sql": rendered, "ms": ms, "rows": min(len(rows), 1000),
                                                          "more": len(rows) > 1000, "columns": columns, "read_only": True})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_parse_statements(sql: list[str], per_minute: float = 60) -> CallToolResult:
    """Workload statements from SQL texts (the SQL Editor's "→ Workload", the chat's "Add to workload"): leading
    ``-- name:`` / ``-- per_minute:`` hint lines name a statement and set its rate (else ``per_minute``)."""
    stmts = wl.statements_from_sql(list(sql), per_minute)
    return tool_result(f"{len(stmts)} statement(s).", {"statements": stmts})


# SQL sent from a SQL Editor outside the Workspace: kept here until the Workload page opened in the browser
# (whose link carries the id) takes it. In memory: a restart drops it, and so does the browser link.
_HANDOFF_TTL_S = 3600
_handoffs: dict[str, dict[str, Any]] = {}
_handoff_lock = threading.Lock()


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_handoff_put(statements: list[dict[str, Any]], database: str | None = None, workload: str | None = None,
                         new_name: str | None = None) -> CallToolResult:
    """Keep statements for a Workload page about to open in the browser (target workload or a new one's name);
    returns the id to put in its link as ``handoff``."""
    if not statements:
        raise ValueError("No statements to send.")
    hid = secrets.token_hex(6)
    with _handoff_lock:
        now = time.time()
        for k in [k for k, v in _handoffs.items() if now - v["t"] > _HANDOFF_TTL_S]:
            _handoffs.pop(k, None)
        while len(_handoffs) >= 20:
            _handoffs.pop(next(iter(_handoffs)))
        _handoffs[hid] = {"t": now, "statements": statements[: wl.MAX_STATEMENTS], "database": database,
                          "workload": workload, "new_name": new_name}
    return tool_result(f"Kept {len(statements)} statement(s) as {hid}.", {"id": hid})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_handoff_take(handoff_id: str) -> CallToolResult:
    """The statements kept by workload_handoff_put (once: they are then removed)."""
    with _handoff_lock:
        h = _handoffs.pop(handoff_id, None)
    if not h:
        raise LookupError("The statements sent from the SQL Editor have expired (the server restarted?); send them again.")
    h.pop("t", None)
    return tool_result(f"{len(h['statements'])} statement(s) from the SQL Editor.", h)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=LOCAL_WRITE)
def workload_update_statement(name: str, statement_id: str, sql: str, database: str | None = None) -> CallToolResult:
    """Write SQL edited in the SQL Editor back into one statement of a saved workload, and save the workload."""
    r = wl.update_statement(name, statement_id, sql, database)
    s = r["statement"]
    return tool_result(f"Updated statement {s['name']!r} [{s['id']}] of workload {name!r} and saved it.", r)


def _chat_context(workload: dict[str, Any], run: dict[str, Any] | None, limit: int = wl.CHAT_CONTEXT_LIMIT) -> str:
    try:
        tables = wl.table_definitions(wl.tables_read(workload))
    except Exception as exc:  # noqa: BLE001 - the chat still works without the table definitions
        tables = {"(table definitions)": {"missing": True, "error": str(exc)}}
    return wl.chat_context(workload, run, tables, limit=max(2000, min(int(limit), wl.CHAT_CONTEXT_LIMIT)))


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_chat_context(workload: dict[str, Any], run: dict[str, Any] | None = None,
                          limit: int = wl.CHAT_CONTEXT_LIMIT) -> CallToolResult:
    """The context the Workload chat sends with a question (workload, last run, table definitions), as text of at
    most ``limit`` characters; for asking in the Claude chat instead of the in-app assistant."""
    text = _chat_context(workload, run, limit)
    return tool_result(f"{len(text)} characters of context.", {"text": text})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_ask(chat_id: str, question: str, workload: dict[str, Any], run: dict[str, Any] | None = None,
                 profile: str | None = None) -> CallToolResult:
    """Ask the in-app assistant about a workload (it may query the database read-only); the answer arrives in
    the chat's inbox (sql_editor_inbox with ``editor_id`` = ``chat_id``)."""
    from .. import assistant
    from .sql_editor import deliver_reply

    if not chat_id.strip() or not question.strip():
        raise ValueError("chat_id and question are required")
    prompt = f"{_chat_context(workload, run)}\n\nQuestion: {question.strip()}"
    assistant.ask(chat_id, prompt, deliver_reply, profile, kind="workload")
    return tool_result("Claude is answering in the Workload view.", {"chat_id": chat_id, "started": True})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_assistant_warm(chat_id: str, profile: str | None = None) -> CallToolResult:
    """Start the Workload chat's assistant process ahead of the first question (no model call)."""
    from .. import assistant

    return tool_result("ok", {"warm": assistant.warm(chat_id, profile, kind="workload")})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def workload_tune(sql: str, database: str | None = None, p50_ms: float | None = None, rows: float | None = None) -> CallToolResult:
    """Tuning recommendations for one workload statement (EXPLAIN, table design), as for a query in the history."""
    from .query_history import _analysis_summary, analyze_event

    a = analyze_event({"key": None, "query_text": sql, "context_database": database, "duration_ms": p50_ms or 0,
                       "row_count": rows, "success": 1, "plan_id": None, "user_name": "workload"})
    return tool_result(_analysis_summary(a), a)
