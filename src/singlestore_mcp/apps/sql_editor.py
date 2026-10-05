"""SQL Editor app: schema tree, SQL editor with autocomplete, and a results pane.

Read-only statements run through ``sql_editor_query`` (the Query Grid's
read-only guard and row cap). Anything else needs the user to confirm in the
editor and then runs through ``sql_editor_execute``, which is marked
destructive so hosts can ask for approval. Both are app-only: the model opens
the editor with ``sql_editor`` and uses run_sql / query_grid itself.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from ..db import db
from ._core import APP_ONLY, apps, jsonable_rows, register_app, tool_result, with_browser_link
from .query_grid import MAX_ROWS_LIMIT, ReadOnlyViolation, check_read_only, run_query

URI = "ui://singlestore/sql-editor.html"
READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
SYSTEM_DATABASES = ("information_schema", "cluster", "memsql")

register_app(
    URI,
    "sql_editor.html",
    name="SQL Editor",
    description="SQL editor with SingleStore autocomplete, schema tree and results",
)


def _databases() -> list[str]:
    rows = db.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")[1]
    names = [r["SCHEMA_NAME"] for r in rows]
    return sorted(names, key=lambda n: (n in SYSTEM_DATABASES, n.lower(), n))


@apps.tool(resource_uri=URI, title="SQL Editor", annotations=READ_ONLY)
def sql_editor(database: str | None = None, sql: str | None = None) -> CallToolResult:
    """Open an interactive SQL editor for SingleStore.

    The user gets a schema tree, an editor with autocomplete for SQL keywords,
    SingleStore functions, databases, tables and columns, and a results pane.
    Use this when the user wants to write, edit or run SQL themselves. To run
    a query for your own reasoning use run_sql; to show results use query_grid.
    The editor has a chat panel: questions from it arrive in the conversation
    tagged "[SQL Editor <id>]"; answer those with sql_editor_reply.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.

    Args:
        database: Database to start in (case-sensitive).
        sql: SQL to put in the editor (not run automatically).
    """
    databases = _databases()
    if database and database not in databases:
        raise LookupError(f"Database {database!r} not found (names are case-sensitive).")
    data: dict[str, Any] = {"databases": databases, "database": database, "sql": sql}
    summary = f"SQL Editor opened{f' on database {database}' if database else ''}."
    if sql:
        summary += f" Prefilled with: {' '.join(sql.split())[:300]}"
    summary = with_browser_link(summary, data, "sql_editor", {"database": database, "sql": sql})
    return tool_result(summary, data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_query(sql: str, database: str | None = None, max_rows: int = 1000) -> CallToolResult:
    """Run a read-only statement from the SQL Editor app.

    Statements that change data or schema are answered with
    ``needs_confirmation`` instead of running; the app then asks the user and
    uses sql_editor_execute.
    """
    try:
        check_read_only(sql)
    except ReadOnlyViolation as exc:
        data = {"needs_confirmation": True, "reason": str(exc), "sql": sql, "database": database}
        return tool_result("This statement changes data or schema and needs confirmation.", data)
    data = run_query(sql, database, max_rows)
    data["kind"] = "read"
    return tool_result(f"{data['row_count']} row(s) in {data['elapsed_ms']} ms", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=WRITES)
def sql_editor_execute(sql: str, database: str | None = None, max_rows: int = 1000) -> CallToolResult:
    """Run any single statement the user confirmed in the SQL Editor app (writes and DDL included)."""
    max_rows = max(1, min(int(max_rows), MAX_ROWS_LIMIT))
    started = time.perf_counter()
    columns, rows, rowcount = db.execute(sql, database=database, max_rows=max_rows + 1)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    data = {
        "kind": "write",
        "sql": sql,
        "database": database,
        "columns": list(dict.fromkeys(columns)),
        "has_result_set": bool(columns),
        "rows": jsonable_rows(rows[:max_rows]),
        "row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
        "affected_rows": rowcount if rowcount is not None and rowcount >= 0 else None,
        "elapsed_ms": elapsed_ms,
        "max_rows": max_rows,
        "warnings": [],
    }
    return tool_result(f"Statement ran in {elapsed_ms} ms; {data['affected_rows']} row(s) affected", data)


# ---------------------------------------------------------------- chat relay
# Questions travel editor -> chat as a user message; answers travel back via
# sql_editor_reply into this per-editor inbox, which the open editor polls.
_INBOX_MAX_MESSAGES = 50
_INBOX_MAX_EDITORS = 50
_inbox: dict[str, list[dict[str, Any]]] = {}
_inbox_lock = threading.Lock()
_inbox_seq = 0


def deliver_reply(editor_id: str, message: str) -> int:
    """Queue a reply for an editor's chat panel; returns its sequence number."""
    global _inbox_seq
    if not editor_id.strip() or not message.strip():
        raise ValueError("editor_id and message are required")
    with _inbox_lock:
        _inbox_seq += 1
        box = _inbox.setdefault(editor_id, [])
        box.append({"seq": _inbox_seq, "t": round(time.time(), 1), "text": message})
        del box[:-_INBOX_MAX_MESSAGES]
        while len(_inbox) > _INBOX_MAX_EDITORS:
            _inbox.pop(next(iter(_inbox)))
        return _inbox_seq


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_inbox(editor_id: str, after: int = 0) -> CallToolResult:
    """Replies sent to one SQL Editor's chat panel, newer than ``after``."""
    with _inbox_lock:
        messages = [m for m in _inbox.get(editor_id, []) if m["seq"] > after]
    return tool_result(f"{len(messages)} new message(s)", {"editor_id": editor_id, "messages": messages})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_schema(database: str) -> CallToolResult:
    """Tables, columns and routines of one database, for the SQL Editor's tree and autocomplete."""
    tables = db.execute(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
        (database,),
    )[1]
    if not tables and database not in _databases():
        raise LookupError(f"Database {database!r} not found (names are case-sensitive).")
    columns = db.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
        (database,),
    )[1]
    routines = db.execute(
        "SELECT ROUTINE_NAME, ROUTINE_TYPE, DATA_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s",
        (database,),
    )[1]
    by_table: dict[str, list[dict[str, Any]]] = {}
    for c in columns:
        by_table.setdefault(c["TABLE_NAME"], []).append(
            {"name": c["COLUMN_NAME"], "type": c["COLUMN_TYPE"], "key": c["COLUMN_KEY"] or None}
        )
    data = {
        "database": database,
        "tables": sorted(
            (
                {"name": t["TABLE_NAME"], "view": t["TABLE_TYPE"] != "BASE TABLE", "columns": by_table.get(t["TABLE_NAME"], [])}
                for t in tables
            ),
            key=lambda t: (t["name"].lower(), t["name"]),
        ),
        "routines": [
            {"name": r["ROUTINE_NAME"], "type": r["ROUTINE_TYPE"], "returns": r["DATA_TYPE"] or None} for r in routines
        ],
    }
    return tool_result(f"{len(data['tables'])} table(s) in {database}", data)
