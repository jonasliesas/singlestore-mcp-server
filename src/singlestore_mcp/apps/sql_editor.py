"""SQL Editor app: schema tree, SQL editor with autocomplete, and a results pane.

Read-only statements run through ``sql_editor_query`` (the Query Grid's
read-only guard and row cap). Anything else needs the user to confirm in the
editor and then runs through ``sql_editor_execute``, which is marked
destructive so hosts can ask for approval. Both are app-only: the model opens
the editor with ``sql_editor`` and uses run_sql / query_grid itself.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ToolAnnotations

from .. import assistant
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

# The workspace shell: SQL editor plus the Schema, Pipelines and Cluster apps
# as views behind a left-hand rail. sql_editor opens it.
WORKSPACE_URI = "ui://singlestore/workspace.html"
WORKSPACE_VIEWS = ("sql", "schema", "pipelines", "cluster")
register_app(
    WORKSPACE_URI,
    "workspace.html",
    name="SingleStore Workspace",
    description="SQL editor with schema explorer, pipeline monitor and cluster monitor views",
)


def _databases() -> list[str]:
    rows = db.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")[1]
    names = [r["SCHEMA_NAME"] for r in rows]
    return sorted(names, key=lambda n: (n in SYSTEM_DATABASES, n.lower(), n))


@apps.tool(resource_uri=WORKSPACE_URI, title="SingleStore Workspace", annotations=READ_ONLY)
def sql_editor(
    database: str | None = None,
    sql: str | None = None,
    view: str = "sql",
    table: str | None = None,
) -> CallToolResult:
    """Open the SingleStore workspace: an interactive SQL editor plus views.

    A slim rail on the left switches between the SQL Editor (schema tree,
    editor with autocomplete for keywords, SingleStore functions, databases,
    tables and columns, and a results pane), the Schema Explorer, the Pipeline
    Monitor and the Cluster Monitor. Use this when the user wants to write,
    edit or run SQL themselves, or wants the combined workspace. To run a
    query for your own reasoning use run_sql; to show results use query_grid.
    The editor has a chat panel: questions from it arrive in the conversation
    tagged "[SQL Editor <id>]"; answer those with sql_editor_reply.

    The result includes ``browser_url``, which opens this view full-window in
    the user's browser: post it as a clickable link right under the app.

    Args:
        database: Database to start in (case-sensitive).
        sql: SQL to put in the editor (not run automatically).
        view: View to show first: "sql" (default), "schema", "pipelines" or "cluster".
        table: For view="schema": table to pre-select in `database`.
    """
    if view not in WORKSPACE_VIEWS:
        raise ValueError(f"view must be one of {', '.join(WORKSPACE_VIEWS)}")
    databases = _databases()
    if database and database not in databases:
        raise LookupError(f"Database {database!r} not found (names are case-sensitive).")
    data: dict[str, Any] = {"databases": databases, "database": database, "sql": sql, "view": view, "table": table}
    summary = f"SingleStore Workspace opened on the {view} view{f', database {database}' if database else ''}."
    if sql:
        summary += f" Editor prefilled with: {' '.join(sql.split())[:300]}"
    summary = with_browser_link(
        summary, data, "sql_editor", {"database": database, "sql": sql, "view": view if view != "sql" else None, "table": table}
    )
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


# ---------------------------------------------------------------- SQL files
# Open/Save work on .sql files in one folder on the machine running this
# server (the user's laptop), so they work inside Claude and in the browser
# view alike. SINGLESTORE_MCP_SQL_DIR overrides the default folder.
_SQL_SUFFIX = ".sql"
_MAX_FILE_BYTES = 2_000_000
_MAX_LISTED = 500


def sql_dir() -> Path:
    configured = os.environ.get("SINGLESTORE_MCP_SQL_DIR")
    if configured:
        return Path(configured).expanduser()
    documents = Path.home() / "Documents"
    return (documents if documents.is_dir() else Path.home()) / "SingleStore SQL"


def _sql_path(name: str) -> Path:
    """Resolve a file name inside the SQL folder; subfolders allowed, nothing outside it."""
    root = sql_dir().resolve()
    name = name.strip().replace("\\", "/")
    if not name or name.endswith("/"):
        raise ValueError("Give the file a name.")
    if not name.lower().endswith(_SQL_SUFFIX):
        name += _SQL_SUFFIX
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Files must stay inside the SQL folder.")
    return path


def _relative(path: Path) -> str:
    return path.relative_to(sql_dir().resolve()).as_posix()


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_files() -> CallToolResult:
    """The .sql files in the SQL Editor's folder, newest first."""
    root = sql_dir()
    files = []
    if root.is_dir():
        for path in root.rglob(f"*{_SQL_SUFFIX}"):
            if path.is_file():
                st = path.stat()
                files.append({"name": _relative(path.resolve()), "size": st.st_size, "modified": round(st.st_mtime)})
    files.sort(key=lambda f: f["modified"], reverse=True)
    data = {"folder": str(root), "files": files[:_MAX_LISTED], "truncated": len(files) > _MAX_LISTED}
    return tool_result(f"{len(files)} SQL file(s) in {root}", data)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_open_file(name: str) -> CallToolResult:
    """Read one .sql file from the SQL Editor's folder."""
    path = _sql_path(name)
    if not path.is_file():
        raise LookupError(f"{_relative(path)} doesn't exist in {sql_dir()}.")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError(f"{_relative(path)} is larger than {_MAX_FILE_BYTES // 1_000_000} MB.")
    raw = path.read_bytes()
    try:
        sql = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sql = raw.decode("cp1252", errors="replace")
    return tool_result(f"Opened {_relative(path)}", {"name": _relative(path), "sql": sql, "folder": str(sql_dir())})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def sql_editor_save_file(name: str, sql: str, overwrite: bool = False) -> CallToolResult:
    """Save the editor's SQL as a .sql file in the SQL Editor's folder.

    An existing file is only replaced with ``overwrite``; otherwise the result
    says ``exists`` so the app can ask first.
    """
    path = _sql_path(name)
    if len(sql.encode("utf-8")) > _MAX_FILE_BYTES:
        raise ValueError(f"The SQL is larger than {_MAX_FILE_BYTES // 1_000_000} MB.")
    if path.exists() and not overwrite:
        return tool_result(f"{_relative(path)} already exists.", {"name": _relative(path), "exists": True, "saved": False})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(sql, encoding="utf-8", newline="")
    os.replace(tmp, path)
    return tool_result(
        f"Saved {_relative(path)}",
        {"name": _relative(path), "saved": True, "exists": False, "folder": str(sql_dir()), "path": str(path)},
    )


# ---------------------------------------------------------------- chat relay
# Questions travel editor -> chat as a user message; answers travel back via
# sql_editor_reply into this per-editor inbox, which the open editor polls.
_INBOX_MAX_MESSAGES = 50
_INBOX_MAX_EDITORS = 50
_inbox: dict[str, list[dict[str, Any]]] = {}
_inbox_lock = threading.Lock()
_inbox_seq = 0


def deliver_reply(editor_id: str, message: str, kind: str = "claude") -> int:
    """Queue a reply ("claude") or a problem ("error"/"note") for an editor's chat panel; returns its sequence number."""
    global _inbox_seq
    if not editor_id.strip() or not message.strip():
        raise ValueError("editor_id and message are required")
    with _inbox_lock:
        _inbox_seq += 1
        box = _inbox.setdefault(editor_id, [])
        box.append({"seq": _inbox_seq, "t": round(time.time(), 1), "text": message, "kind": kind})
        del box[:-_INBOX_MAX_MESSAGES]
        while len(_inbox) > _INBOX_MAX_EDITORS:
            _inbox.pop(next(iter(_inbox)))
        return _inbox_seq


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_inbox(editor_id: str, after: int = 0) -> CallToolResult:
    """Replies sent to one SQL Editor's chat panel, newer than ``after``."""
    with _inbox_lock:
        messages = [m for m in _inbox.get(editor_id, []) if m["seq"] > after]
    data = {"editor_id": editor_id, "messages": messages, "job": assistant.job_state(editor_id)}
    return tool_result(f"{len(messages)} new message(s)", data)


# In-app answers: headless Claude Code with read-only database tools (see
# singlestore_mcp.assistant). Works in the browser view too, and needs no
# Send click in the Claude chat.
@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_assistant() -> CallToolResult:
    """Whether the SQL Editor can answer questions in the app (Claude Code installed)."""
    info = assistant.status()
    return tool_result("available" if info["available"] else info["reason"], info)


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_ask(
    editor_id: str,
    question: str,
    database: str | None = None,
    sql: str | None = None,
    selected: bool = False,
    last_result: str | None = None,
    profile: str | None = None,
) -> CallToolResult:
    """Answer a SQL Editor chat question in the app; the reply arrives in the editor's inbox.

    ``profile`` picks speed vs depth: "fast", "balanced" (default) or "thorough".
    """
    if not editor_id.strip() or not question.strip():
        raise ValueError("editor_id and question are required")
    prompt = assistant.build_prompt(question, database, sql, selected, last_result)
    assistant.ask(editor_id, prompt, deliver_reply, profile)
    return tool_result("Claude is answering in the editor.", {"editor_id": editor_id, "started": True})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_assistant_warm(editor_id: str, profile: str | None = None) -> CallToolResult:
    """Start the editor's assistant process ahead of the first question (no model call)."""
    started = assistant.warm(editor_id, profile)
    return tool_result("Assistant ready." if started else "Assistant unavailable.", {"warm": started})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_ask_cancel(editor_id: str) -> CallToolResult:
    """Stop the in-app answer that is running for one SQL Editor."""
    stopped = assistant.cancel(editor_id)
    return tool_result("Stopped." if stopped else "Nothing to stop.", {"stopped": stopped})


@apps.tool(resource_uri=URI, visibility=APP_ONLY, annotations=READ_ONLY)
def sql_editor_schema(database: str) -> CallToolResult:
    """Tables, columns and routines of one database, for the SQL Editor's tree and autocomplete."""
    tables, columns, routines = db.parallel(
        lambda: db.execute(
            "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
            (database,),
        )[1],
        lambda: db.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (database,),
        )[1],
        lambda: db.execute(
            "SELECT ROUTINE_NAME, ROUTINE_TYPE, DATA_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s",
            (database,),
        )[1],
    )
    if not tables and database not in _databases():
        raise LookupError(f"Database {database!r} not found (names are case-sensitive).")
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
